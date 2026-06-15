from flask import Flask, render_template, request, jsonify, send_file
import os, uuid, threading, subprocess, tempfile, asyncio, time
from pathlib import Path

from converter import extract_text, clean_text, split_into_chunks, chunk_to_mp3

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB max upload

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# In-memory job store: job_id -> {status, progress, total, output, error, created_at}
jobs = {}

VOICES = [
    {"id": "en-US-AriaNeural",    "label": "Aria",    "desc": "US · Female · Warm"},
    {"id": "en-US-JennyNeural",   "label": "Jenny",   "desc": "US · Female · Friendly"},
    {"id": "en-US-GuyNeural",     "label": "Guy",     "desc": "US · Male · Confident"},
    {"id": "en-GB-RyanNeural",    "label": "Ryan",    "desc": "British · Male"},
    {"id": "en-GB-SoniaNeural",   "label": "Sonia",   "desc": "British · Female"},
    {"id": "en-AU-NatashaNeural", "label": "Natasha", "desc": "Australian · Female"},
]


def run_conversion(job_id, input_path, output_path, voice, rate):
    try:
        jobs[job_id].update(status="extracting")

        text = extract_text(input_path)
        text = clean_text(text)
        chunks = split_into_chunks(text)
        total = len(chunks)
        jobs[job_id].update(status="converting", total=total, progress=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            chunk_files = []
            for i, chunk in enumerate(chunks):
                chunk_path = os.path.join(tmpdir, f"seg_{i:05d}.mp3")
                asyncio.run(chunk_to_mp3(chunk, chunk_path, voice, rate, "+0Hz"))
                jobs[job_id]["progress"] = i + 1
                chunk_files.append(chunk_path)

            jobs[job_id]["status"] = "merging"

            list_file = os.path.join(tmpdir, "list.txt")
            with open(list_file, "w") as f:
                for cf in chunk_files:
                    f.write(f"file '{cf}'\n")

            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", list_file, "-c", "copy", output_path],
                capture_output=True, check=True
            )

        jobs[job_id].update(status="done", output=output_path)

    except Exception as e:
        jobs[job_id].update(status="error", error=str(e))
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass


def cleanup_old_jobs():
    """Delete output files and job entries older than 1 hour."""
    while True:
        time.sleep(3600)
        cutoff = time.time() - 3600
        for jid in list(jobs.keys()):
            job = jobs.get(jid)
            if job and job.get("created_at", 0) < cutoff:
                out = job.get("output")
                if out:
                    try:
                        os.unlink(out)
                    except OSError:
                        pass
                jobs.pop(jid, None)


# Start background cleanup thread
threading.Thread(target=cleanup_old_jobs, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", voices=VOICES)


@app.route("/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify(error="No file uploaded"), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify(error="No file selected"), 400

    voice = request.form.get("voice", "en-US-AriaNeural")
    rate  = request.form.get("rate", "+0%")

    job_id   = str(uuid.uuid4())
    ext      = Path(f.filename).suffix.lower()
    in_path  = str(UPLOAD_DIR / f"{job_id}{ext}")
    out_path = str(UPLOAD_DIR / f"{job_id}.mp3")

    f.save(in_path)
    jobs[job_id] = {"status": "queued", "progress": 0, "total": 0, "created_at": time.time()}

    threading.Thread(
        target=run_conversion,
        args=(job_id, in_path, out_path, voice, rate),
        daemon=True
    ).start()

    return jsonify(job_id=job_id)


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify({k: v for k, v in job.items() if k != "output"})


@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Not ready"), 404
    return send_file(job["output"], as_attachment=True, download_name="audiobook.mp3")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
