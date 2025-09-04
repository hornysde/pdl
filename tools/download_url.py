import asyncio
import hashlib
import logging
import sys

import yt_dlp  # type: ignore[import-untyped]
import pyffmpeg  # type: ignore[import-untyped]

worker_count = 3


async def download_worker(queue: asyncio.Queue):
    ydl_opts = {
        "ffmpeg_location": pyffmpeg.FFmpeg().get_ffmpeg_bin(),
        "format": "bestvideo*+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 10,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as dl:
        while True:
            try:
                url = await queue.get()
            except asyncio.exceptions.CancelledError:
                # worker cancelled
                return
            hash = hashlib.new("md5", url.encode("utf-8")).hexdigest()[:5]
            dl.params["outtmpl"] = {
                "default": f"downloads/from_url/%(title)s_{hash}.%(ext)s",
            }
            try:
                await asyncio.to_thread(lambda: dl.download([url]))
            except Exception as e:
                print(str(e))
            finally:
                queue.task_done()
            print(f"Remaining: {queue.qsize()}")


async def stdin_producer(queue: asyncio.Queue):
    print("Paste URLs (Ctrl+D to finish):")
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            break
        url = line.strip()
        if url == "":
            continue
        await queue.put(url)


async def main():
    # Shut pyffmpeg up
    logging.getLogger("pyffmpeg").handlers = []

    queue: asyncio.Queue = asyncio.Queue()
    workers = [asyncio.create_task(download_worker(queue)) for _ in range(worker_count)]
    await stdin_producer(queue)
    print("Exiting, waiting for download to complete...")
    await queue.join()
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers)


if __name__ == "__main__":
    asyncio.run(main())
