import array
import asyncio
import time

import pyaudio

import config


def rms_level(pcm: bytes) -> float:
    """0..1 loudness of 16-bit PCM."""
    if len(pcm) < 2:
        return 0.0
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    a = a[::4]  # subsample, cheap
    r = (sum(x * x for x in a) / max(len(a), 1)) ** 0.5
    return min(1.0, r / 6000)


class Mic:
    def __init__(self, pa: pyaudio.PyAudio):
        info = pa.get_default_input_device_info()
        self.stream = pa.open(
            format=pyaudio.paInt16, channels=1, rate=config.MIC_RATE,
            input=True, input_device_index=info["index"],
            frames_per_buffer=config.CHUNK,
        )

    async def read(self) -> bytes:
        return await asyncio.to_thread(
            self.stream.read, config.CHUNK, exception_on_overflow=False
        )


class Speaker:
    """Queued, interruptible PCM playback."""

    def __init__(self, pa: pyaudio.PyAudio, on_level=None):
        self.pa = pa
        self.streams = {}
        self.q: asyncio.Queue = asyncio.Queue()
        self.on_level = on_level or (lambda v: None)
        self.last_write = 0.0

    def _stream(self, rate):
        if rate not in self.streams:
            self.streams[rate] = self.pa.open(
                format=pyaudio.paInt16, channels=1, rate=rate, output=True
            )
        return self.streams[rate]

    def enqueue(self, pcm: bytes, rate: int = config.SPK_RATE):
        step = rate // 20 * 2  # 50 ms pieces: fast interrupts + smooth lip-sync
        for i in range(0, len(pcm), step):
            self.q.put_nowait((pcm[i:i + step], rate))

    def clear(self):
        while not self.q.empty():
            self.q.get_nowait()

    @property
    def speaking(self) -> bool:
        return not self.q.empty() or (time.time() - self.last_write) < 0.35

    async def run(self):
        while True:
            pcm, rate = await self.q.get()
            self.on_level(rms_level(pcm))
            await asyncio.to_thread(self._stream(rate).write, pcm)
            self.last_write = time.time()
            if self.q.empty():
                self.on_level(0.0)
