import pyaudio
import queue
import threading
import logging
from fractions import Fraction
import numpy as np
from aiortc import AudioStreamTrack
from av import AudioFrame

log = logging.getLogger(__name__)

class AudioTrack(AudioStreamTrack):
    def __init__(self, sample_rate=48000, channels=2):
        super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples_per_frame = int(sample_rate * 0.02)  # 20ms frames
        self.frame_count = 0
        try:
            self.audio = pyaudio.PyAudio()
            self.stream = None
            self.audio_queue = queue.Queue(maxsize=100)
            self.is_capturing = False
            self.audio_available = True
            self.capture_thread = threading.Thread(target=self._capture_audio, daemon=True)
            self.capture_thread.start()
            log.info(f"AudioTrack initialized with {sample_rate}Hz, {channels} channels")
        except Exception as e:
            log.error(f"Failed to initialize audio: {e}")
            self.audio_available = False
            self.audio = None
            self.stream = None
            self.audio_queue = None
            self.is_capturing = False

    def _capture_audio(self):
        if not self.audio_available:
            log.warning("Audio not available, running in silence mode")
            return
        try:
            try:
                self.stream = self.audio.open(
                    format=pyaudio.paFloat32,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.samples_per_frame,
                    stream_callback=self._audio_callback
                )
            except Exception as channel_err:
                log.warning(f"Stereo audio input opening failed ({channel_err}); falling back to mono (1 channel)")
                self.channels = 1
                self.stream = self.audio.open(
                    format=pyaudio.paFloat32,
                    channels=1,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.samples_per_frame,
                    stream_callback=self._audio_callback
                )
            self.is_capturing = True
            self.stream.start_stream()
            while self.is_capturing:
                try:
                    audio_data = self.audio_queue.get(timeout=1)
                    if audio_data is None:
                        break
                except queue.Empty:
                    continue
        except Exception as e:
            log.warning(f"Audio device capture failed: {e}; operating in silent audio mode")
            self.audio_available = False
        finally:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except:
                    pass
            if self.audio:
                try:
                    self.audio.terminate()
                except:
                    pass

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if self.is_capturing and self.audio_available:
            try:
                audio_array = np.frombuffer(in_data, dtype=np.float32)
                self.audio_queue.put(audio_array)
            except Exception as e:
                log.error(f"Audio callback error: {e}")
        return (None, pyaudio.paContinue)

    async def next_timestamp(self) -> tuple[int, Fraction]:
        if not hasattr(self, "_pts"):
            self._pts = 0
        else:
            self._pts += self.samples_per_frame
        return self._pts, Fraction(1, self.sample_rate)

    async def recv(self):
        try:
            pts, time_base = await self.next_timestamp()
            if not self.audio_available or not self.audio_queue or self.audio_queue.empty():
                silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
                audio_frame = AudioFrame.from_ndarray(
                    silence.reshape(1, -1),
                    format='s16',
                    layout='stereo'
                )
                audio_frame.pts = pts
                audio_frame.time_base = time_base
                audio_frame.sample_rate = self.sample_rate
                return audio_frame
            
            audio_data = self.audio_queue.get_nowait()
            audio_int16 = (audio_data * 32767).astype(np.int16)
            audio_frame = AudioFrame.from_ndarray(
                audio_int16.reshape(1, -1),
                format='s16',
                layout='stereo'
            )
            audio_frame.pts = pts
            audio_frame.time_base = time_base
            audio_frame.sample_rate = self.sample_rate
            self.frame_count += 1
            return audio_frame
        except queue.Empty:
            pts, time_base = await self.next_timestamp()
            silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
            audio_frame = AudioFrame.from_ndarray(
                silence.reshape(1, -1),
                format='s16',
                layout='stereo'
            )
            audio_frame.pts = pts
            audio_frame.time_base = time_base
            audio_frame.sample_rate = self.sample_rate
            return audio_frame
        except Exception as e:
            log.error(f"Audio recv error: {e}")
            pts, time_base = await self.next_timestamp()
            silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
            audio_frame = AudioFrame.from_ndarray(
                silence.reshape(1, -1),
                format='s16',
                layout='stereo'
            )
            audio_frame.pts = pts
            audio_frame.time_base = time_base
            audio_frame.sample_rate = self.sample_rate
            return audio_frame

    def stop(self):
        self.is_capturing = False

