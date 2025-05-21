from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify, after_this_request
from flask_socketio import SocketIO
import os
import yt_dlp
import uuid
import ffmpeg
import re
import time
import threading

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'downloads'
socketio = SocketIO(app, cors_allowed_origins="*")  # Tambahkan ini

# Pastikan folder downloads ada
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Buat progress hook untuk yt-dlp
def progress_hook(d):
    if d['status'] == 'downloading':
        # Hitung persentase
        if d.get('total_bytes'):
            percentage = (d['downloaded_bytes'] / d['total_bytes']) * 100
        elif d.get('total_bytes_estimate'):
            percentage = (d['downloaded_bytes'] / d['total_bytes_estimate']) * 100
        else:
            percentage = 0
            
        # Ambil speed
        speed = d.get('speed', 0)
        if speed:
            speed_mb = round(speed / (1024 * 1024), 2)
            speed_str = f"{speed_mb} MB/s"
        else:
            speed_str = "Unknown speed"
            
        # Kirim ke client
        socketio.emit('download_progress', {
            'percentage': round(percentage, 1),
            'speed': speed_str,
            'downloaded': d.get('downloaded_bytes', 0),
            'total': d.get('total_bytes') or d.get('total_bytes_estimate', 0),
            'filename': d.get('filename', '')
        })
    
    elif d['status'] == 'finished':
        socketio.emit('download_complete', {
            'filename': d.get('filename', '')
        })
        
@app.route('/', methods=['GET', 'POST'])
def index():
    cleanup_old_files()  # Panggil fungsi untuk membersihkan file lama
    if request.method == 'POST':
        youtube_url = request.form['youtube_url']
        start = request.form['start']
        end = request.form['end']
        quality = request.form['quality']
        format = request.form['format']
        filename = request.form.get('filename') or str(uuid.uuid4())
        session_id = request.form.get('session_id')  # Tambah session ID untuk tracking

        download_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}.%(ext)s")

        ydl_opts = {
            'format': quality,
            'outtmpl': download_path,
            'progress_hooks': [progress_hook],  # Tambahkan progress hook
        }

        def process_video():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=True)
                    full_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{filename}.{info['ext']}")
                    cut_path = os.path.join(app.config['UPLOAD_FOLDER'], f"cut_{filename}.{format}")
                    
                    socketio.emit('processing_start', {'session_id': session_id})
                    
                    # ffmpeg cut
                    (
                        ffmpeg
                        .input(full_path, ss=start, to=end)
                        .output(cut_path)
                        .run(overwrite_output=True)
                    )
                    
                    # PERBAIKAN: Kirim nama file yang benar
                    cut_filename = f"cut_{filename}.{format}"
                    socketio.emit('processing_complete', {
                        'session_id': session_id,
                        'download_url': f'/download/{cut_filename}'
                    })
                    
            except Exception as e:
                socketio.emit('processing_error', {
                    'session_id': session_id,
                    'error': str(e)
                })
        
        # Mulai proses di thread terpisah
        thread = threading.Thread(target=process_video)
        thread.daemon = True
        thread.start()
        
        return jsonify({'status': 'processing', 'session_id': session_id})
    
    return render_template('index.html')

# Route untuk mengunduh file yang sudah dipotong
@app.route('/download/<filename>')
def download(filename):
    # Path lengkap ke file
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Pastikan file ada
    if not os.path.exists(file_path):
        print(f"File tidak ditemukan: {file_path}")
        return jsonify({'error': 'File not found'}), 404
    
    @after_this_request
    def cleanup(response):
        try:
            # Hapus file hasil potongan setelah selesai diunduh
            if os.path.exists(file_path):
                # Jangan hapus file segera, tunggu beberapa saat untuk memastikan download berhasil
                # Dalam produksi, gunakan mekanisme lain untuk pembersihan
                pass
        except Exception as e:
            print(f"Error cleaning up file: {e}")
        return response
    
    print(f"Mengirimkan file: {file_path}")
    return send_file(file_path, as_attachment=True)

@app.route('/get_formats', methods=['POST'])
def get_formats():
    youtube_url = request.form['youtube_url']
    
    # Configuration to only extract information without downloading
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            
            # Tambahkan informasi durasi video jika tersedia
            duration = info.get('duration', 0)
            
            formats = []
            
            # Kategorikan format
            video_formats = {}
            audio_formats = {}
            
            # Minimum resolution (480p)
            MIN_RESOLUTION = 480
            
            # First pass: kategorikan format
            for f in info['formats']:
                format_id = f['format_id']
                
                # Periksa format video dan audio
                has_video = f.get('vcodec', 'none') != 'none'
                has_audio = f.get('acodec', 'none') != 'none'
                height = f.get('height', 0)
                
                # Simpan format video saja untuk penggabungan nanti (hanya 480p ke atas)
                if has_video and not has_audio and height and height >= MIN_RESOLUTION:
                    video_formats[format_id] = f
                
                # Simpan format audio saja untuk penggabungan nanti
                elif has_audio and not has_video:
                    audio_formats[format_id] = f
                
                # Tambahkan format yang sudah memiliki video dan audio (hanya 480p ke atas)
                elif has_video and has_audio and height and height >= MIN_RESOLUTION:
                    # Format kombinasi dari YouTube
                    resolution = f"{height}p"
                    format_note = f.get('format_note', '')
                    
                    # Hitung ukuran file dalam MB jika tersedia
                    filesize = f.get('filesize')
                    if filesize is None:
                        filesize = f.get('filesize_approx')
                        
                    if filesize:
                        filesize_mb = round(filesize / (1024 * 1024), 2)
                        size_str = f"{filesize_mb} MB"
                    else:
                        size_str = "Unknown size"

                    description = f"{resolution}"
                    if format_note:
                        description += f" ({format_note})"
                    description += f" - {size_str}"
                    
                    formats.append({
                        'format_id': format_id,
                        'description': description,
                        'ext': f.get('ext', ''),
                        'height': height,
                        'filesize': filesize or 0,
                        'combined': True  # Flag untuk menandai format kombinasi
                    })
            
            # Tambahkan format gabungan (video+audio terbaik)
            if audio_formats:  # Pastikan ada format audio untuk digabungkan
                best_audio = max(audio_formats.values(), key=lambda x: x.get('filesize', 0)) if audio_formats else None
                audio_size = best_audio.get('filesize', 0) if best_audio else 0
                
                # Kelompokkan video formats berdasarkan height
                video_by_height = {}
                for video_id, video in video_formats.items():
                    height = video.get('height', 0)
                    if height:
                        if height not in video_by_height:
                            video_by_height[height] = []
                        video_by_height[height].append((video_id, video))
                
                # Untuk setiap resolusi, pilih video dengan filesize terbesar (kualitas terbaik)
                for height, videos in video_by_height.items():
                    if height >= MIN_RESOLUTION:
                        # Sort videos by filesize (descending)
                        videos.sort(key=lambda x: x[1].get('filesize', 0), reverse=True)
                        
                        # Ambil video dengan filesize terbesar
                        best_video_id, best_video = videos[0]
                        
                        resolution = f"{height}p"
                        format_note = best_video.get('format_note', '')
                        
                        # Hitung ukuran file jika tersedia
                        filesize = best_video.get('filesize')
                        if filesize is None:
                            filesize = best_video.get('filesize_approx')
                        
                        # Estimasi ukuran gabungan
                        if filesize and audio_size:
                            total_filesize = filesize + audio_size
                            filesize_mb = round(total_filesize / (1024 * 1024), 2)
                            size_str = f"{filesize_mb} MB (estimated)"
                        else:
                            size_str = "Unknown size"
                        
                        description = f"{resolution}"
                        if format_note:
                            description += f" ({format_note})"
                        description += f" - {size_str}"
                        
                        formats.append({
                            'format_id': f"{best_video_id}+bestaudio",
                            'description': description,
                            'ext': 'mp4',  # Default extension for merged
                            'height': height,
                            'filesize': total_filesize if filesize and audio_size else 0,
                            'combined': False  # Flag untuk menandai format gabungan
                        })
            
            # Hilangkan duplikasi berdasarkan height
            # Untuk setiap resolusi, prioritaskan format kombinasi jika ada
            unique_formats = {}
            for f in formats:
                height = f.get('height', 0)
                filesize = f.get('filesize', 0)
                is_combined = f.get('combined', False)
                
                if height not in unique_formats:
                    unique_formats[height] = f
                else:
                    # Jika format baru adalah combined dan format lama bukan, atau
                    # format baru memiliki filesize lebih besar
                    if (is_combined and not unique_formats[height].get('combined', False)) or \
                       (filesize > unique_formats[height].get('filesize', 0)):
                        unique_formats[height] = f
            
            # Convert dictionary back to list
            filtered_formats = list(unique_formats.values())
            
            # Sort formats by height (higher first)
            filtered_formats.sort(key=lambda x: x.get('height', 0), reverse=True)
            
            # Keluarkan informasi debug
            print(f"Found {len(formats)} raw formats, filtered to {len(filtered_formats)} unique formats")
            
            return jsonify({
                'title': info.get('title', 'Unknown Title'),
                'formats': filtered_formats,
                'duration': duration  # Tambahkan durasi video
            })
    except Exception as e:
        print(f"Error getting formats: {str(e)}")
        return jsonify({'error': str(e)}), 400

def cleanup_old_files():
    """Hapus semua file yang berumur lebih dari 1 jam"""
    now = time.time()
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        # Jika file berumur lebih dari 1 jam (3600 detik)
        if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 3600:
            try:
                os.remove(file_path)
                print(f"Deleted old file: {file_path}")
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")



if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, debug=True)