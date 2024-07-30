import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT # <-- ADD THIS LINE
import os
import sys
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(parent_dir)
from model.model import EmbeddingModel
import torchaudio.transforms as T
from torchvision.transforms import v2
from librosa.util import fix_length
import librosa
import torch
import csv
from collections import defaultdict

mp3_data_path = '/mnt/c/Users/User/Documents/NeuraBeat/fma_small/'
csv_path = '/mnt/c/Users/User/Documents/NeuraBeat/metadata/fma_metadata/tracks.csv'
embedding_model_path = '../model/embedding_model_loss.pt'

conn = psycopg2.connect(
    dbname=os.getenv('DB_NAME'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    host=os.getenv('DB_HOST')
)

def create_table(conn):
    cur = conn.cursor()
    cur.execute("""
                CREATE TABLE song_embeddings (
                    id bigserial PRIMARY KEY,
                    song_name TEXT NOT NULL,
                    genre TEXT,
                    embedding vector(128)
                );
                """)

    cur.execute("""
                ALTER TABLE song_embeddings ADD CONSTRAINT unique_embedding UNIQUE (embedding);
                """)

    conn.commit()
    cur.close()

def delete_table(conn):
    cur = conn.cursor()
    cur.execute("""
                DROP TABLE song_embeddings;
                """)
    conn.commit()
    cur.close()

def insert_embedding(conn, song_name, genre, embedding):
    cur = conn.cursor()
    cur.execute("""
                INSERT INTO song_embeddings (song_name, genre, embedding)
                VALUES (%s, %s, %s)
                ON CONFLICT (embedding) DO NOTHING;
                """, (song_name, genre, embedding))
    conn.commit()
    cur.close()

def embedding_exists(conn, embedding):
    cur = conn.cursor()
    embedding = '[' + ','.join(map(str, embedding)) + ']'
    cur.execute("""
                SELECT id, song_name, genre
                FROM song_embeddings
                WHERE embedding <-> %s < 0.0005;  -- Adjust the threshold as needed
                """, (embedding,))
    exists = cur.fetchone()
    if exists:
        print(f"Embedding exists with ID: {exists[0]}, Song Name: {exists[1]}, Genre: {exists[2]}")
    cur.close()
    return exists

def retrieve_similar_embeddings(conn, embedding):
    cur = conn.cursor()
    embedding = '[' + ','.join(map(str, embedding)) + ']'
    
    cur.execute("""
                SELECT song_name, genre, (embedding <-> %s) AS distance
                FROM song_embeddings
                ORDER BY embedding <-> %s
                LIMIT 5;
                """, (embedding, embedding))
    rows = cur.fetchall()
    
    embeddings_with_distances = [(row[0], row[1], row[2]) for row in rows]
    cur.close()
    return embeddings_with_distances


def create_file_genre_map(mp3_data_path, csv_path):
    file_genre_map = {} 
    track_ids = [file_name.split('.')[0].lstrip('0') for file_name in os.listdir(mp3_data_path) if file_name.endswith('.mp3')]

    with open(csv_path, 'r') as csvfile:
        csvreader = csv.reader(csvfile)
        next(csvreader) 
        next(csvreader)
        next(csvreader)
        for row in csvreader:
            if row[0] in track_ids:
                genre = row[40]
                file_genre_map[row[0]] = genre
    
    return file_genre_map

def insert_all_embeddings(model_path, mp3_data_path, csv_path, file_genre_map, conn):
    cur = conn.cursor()
    genre_counts = defaultdict(int)
    max_songs_per_genre = 990
    target_sr = 16000
    n_mels=128
    n_fft=2048
    hop_length=512
    mean=6.5304
    std=11.8924
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = EmbeddingModel()
    model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))
    model.to(device)
    model.eval()

    chunk_duration = 3
    full_song_length = 27
    num_chunks = full_song_length // chunk_duration

    for mp3_file in os.listdir(mp3_data_path):
        track_id = mp3_file.split('.')[0].lstrip('0')
        genre = file_genre_map[track_id]
        if genre_counts[genre] >= max_songs_per_genre:
            continue

        try:    
            audio, sr = librosa.load(os.path.join(mp3_data_path, mp3_file))
            if (len(audio) / sr) < full_song_length:
                print(f"Skipped short file: {mp3_file}")
                continue
            resampled_audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr)
            padded_audio = fix_length(resampled_audio, size=target_sr * full_song_length)

            chunk_length = target_sr * chunk_duration
            for i in range(num_chunks):
                start_sample = i * chunk_length
                end_sample = start_sample + chunk_length
                if end_sample > len(padded_audio):
                    break
                audio_chunk = torch.tensor(padded_audio[start_sample:end_sample]).unsqueeze(0)

                mel_spec = T.MelSpectrogram(sample_rate=target_sr, n_mels=n_mels, n_fft=n_fft, hop_length=hop_length)(audio_chunk)
                log_mel_spec = T.AmplitudeToDB()(mel_spec)
                mel_spec_tensor = log_mel_spec.unsqueeze(0)
                mel_spec_tensor = v2.Compose([v2.Compose([v2.ToImage(), v2.ToDtype(torch.float32, scale=True)]),
                                            v2.Normalize((mean,), (std,))])(mel_spec_tensor)

                with torch.no_grad():
                    embedding = model(mel_spec_tensor)
                embedding = embedding.flatten().detach().cpu().numpy().tolist()

                song_name = track_id + "_c" + str(i)

                cur.execute("""
                    INSERT INTO song_embeddings (song_name, genre, embedding)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (embedding) DO NOTHING;
                """, (song_name, genre, embedding))
                conn.commit()
                if cur.rowcount == 0:
                    print("Skipped: Embedding already exists in the database.")
                else:
                    print("Inserted track", song_name)

            genre_counts[genre] += 1

        except Exception as e:
            print(e)
            print(f"Skipped corrupt file: {mp3_file}")

    cur.close()
    conn.close()