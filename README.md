# FDM – Free Download Manager
### A real, working IDM alternative — runs locally on your PC

## What it does
- Actually downloads files from the internet to your disk
- Multi-segment downloading (splits file into 8 parts, downloads in parallel = faster)
- Real-time progress via SSE (Server-Sent Events)
- Pause / Resume / Stop support
- Live speed graph
- Saves to ~/Downloads/FDM/ by default

## Requirements
- Python 3.8+
- pip

## Setup (one time)

```bash
pip install flask requests
```

## Run

```bash
python server.py
```

Then open your browser at: **http://localhost:6800**

## How to use
1. Paste any direct download URL into the URL bar
2. Click "Add Download" or press Enter
3. Set segments (8 = fastest for most servers), click Start
4. Watch it download in real time!

## Notes
- Works with any direct file URL (not streaming sites like YouTube)
- Files are saved to: ~/Downloads/FDM/
- To change the folder, edit `DOWNLOAD_DIR` in server.py
- Multi-segment only works if the server supports HTTP Range requests
  (most file hosts do — Google Drive, GitHub, direct CDN links, etc.)

## Tips for fast downloads
- Use 8-16 segments for large files
- Works great with: ISO files, ZIP archives, EXE installers, PDFs, etc.
- For YouTube/streaming, use yt-dlp separately
