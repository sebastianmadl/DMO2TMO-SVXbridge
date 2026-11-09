"""ACELP-Codec ueber ctypes.

Die Bibliotheken baut `codec/build.sh` (holt den ETSI-Referenzcode zur
Buildzeit). Fehlen sie, wirft der Konstruktor `CodecUnavailable` - der Rest
des Programms laeuft weiter, damit selftest/smoketest auch ohne Codec
gruen bleiben.

Bitreihenfolge: der ETSI-Code will 137 Woerter mit je 0 oder 1, MSB
zuerst. `split_traffic_frame()` liefert die Subframes als 137-Bit-Integer,
ebenfalls MSB zuerst - die Abbildung ist damit 1:1 ohne Umsortieren.

Encoder und Decoder sind absichtlich zwei getrennte .so-Dateien, weil sie
sich sonst den Zustand der praediktiven Gain-Quantisierung teilen wuerden
(Details in codec/tetra_wrapper.c).

Der ETSI-Code haelt seinen Zustand in globalen Variablen, nicht pro
Instanz - und `dlopen` gibt fuer denselben Pfad denselben Handle zurueck.
Zwei `AcelpCodec`-Objekte wuerden sich den Zustand also teilen und sich
gegenseitig verfaelschen, sobald zwei Reflektoren gleichzeitig Verkehr
haben (gemessen: Korrelation 0.37 statt 1.00). Darum laedt jede Instanz
ihre **eigene Kopie** der .so - andere Inode, eigener Ladevorgang, eigene
globale Variablen.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

SUBFRAME_SAMPLES = 240      # 30 ms bei 8 kHz
SUBFRAME_BITS = 137
FRAME_SAMPLES = 480         # 60 ms = ein Brew-Traffic-Frame = 2 Subframes

log = logging.getLogger("acelp")

LIB_ENC = "libtetraacelp_enc.so"
LIB_DEC = "libtetraacelp_dec.so"


class CodecUnavailable(RuntimeError):
    """Die Bibliotheken sind nicht da oder passen nicht zum Wrapper."""


def _search_dirs() -> list[Path]:
    out = []
    env = os.environ.get("SVXTETRA_ACELP_DIR")
    if env:
        out.append(Path(env))
    root = Path(__file__).resolve().parents[2]
    out.append(root / "codec")
    out.append(Path("/opt/dmo2tmo-svxbridge/lib"))
    out.append(Path("/opt/svxtetra/lib"))      # aeltere Images
    return out


def find_libraries() -> Optional[tuple[Path, Path]]:
    """Verzeichnis suchen, in dem beide .so liegen."""
    for directory in _search_dirs():
        enc, dec = directory / LIB_ENC, directory / LIB_DEC
        if enc.is_file() and dec.is_file():
            return enc, dec
    return None


def available() -> bool:
    return find_libraries() is not None


class AcelpCodec:
    def __init__(self, encoder: Optional[Path] = None,
                 decoder: Optional[Path] = None, private: bool = True):
        if encoder is None or decoder is None:
            found = find_libraries()
            if found is None:
                raise CodecUnavailable(
                    f"{LIB_ENC} / {LIB_DEC} nicht gefunden - erst "
                    f"codec/build.sh laufen lassen. Gesucht in: "
                    f"{', '.join(str(d) for d in _search_dirs())}"
                )
            encoder, decoder = found

        self.enc_path, self.dec_path = Path(encoder), Path(decoder)
        laden = self._load_private if private else self._load
        self.enc = laden(self.enc_path)
        self.dec = laden(self.dec_path)
        self._bind()
        self._verify(self.enc, self.enc_path)
        self._verify(self.dec, self.dec_path)

        self.enc.tetra_enc_init()
        self.dec.tetra_dec_init()

    @staticmethod
    def _load(path: Path) -> ctypes.CDLL:
        try:
            return ctypes.CDLL(str(path))
        except OSError as exc:
            raise CodecUnavailable(f"{path} laedt nicht: {exc}") from exc

    @classmethod
    def _load_private(cls, path: Path) -> ctypes.CDLL:
        """Eine eigene Kopie laden, damit der globale Zustand nicht geteilt wird.

        `dlopen` schaut auf die Inode, nicht auf den Inhalt: eine Kopie wird
        darum getrennt geladen und bekommt eigene globale Variablen. Die
        Datei wird gleich nach dem Laden geloescht - die Abbildung im
        Speicher bleibt bestehen, und es liegt nichts herum.
        """
        verzeichnis = tempfile.mkdtemp(prefix="acelp-")
        kopie = Path(verzeichnis) / path.name
        try:
            shutil.copy2(path, kopie)
            return cls._load(kopie)
        finally:
            for aufraeumen in (lambda: kopie.unlink(),
                               lambda: os.rmdir(verzeichnis)):
                try:
                    aufraeumen()
                except OSError:
                    pass

    def _bind(self) -> None:
        i16 = ctypes.c_int16
        p16 = ctypes.POINTER(i16)

        self.enc.tetra_enc_init.restype = None
        self.enc.tetra_enc_init.argtypes = []
        self.enc.tetra_enc.restype = None
        self.enc.tetra_enc.argtypes = [p16, p16]

        self.dec.tetra_dec_init.restype = None
        self.dec.tetra_dec_init.argtypes = []
        self.dec.tetra_dec.restype = None
        self.dec.tetra_dec.argtypes = [p16, i16, p16]

        for lib in (self.enc, self.dec):
            lib.tetra_codec_frame_samples.restype = ctypes.c_int
            lib.tetra_codec_frame_samples.argtypes = []
            lib.tetra_codec_frame_bits.restype = ctypes.c_int
            lib.tetra_codec_frame_bits.argtypes = []

    @staticmethod
    def _verify(lib: ctypes.CDLL, path: Path) -> None:
        samples = lib.tetra_codec_frame_samples()
        bits = lib.tetra_codec_frame_bits()
        if (samples, bits) != (SUBFRAME_SAMPLES, SUBFRAME_BITS):
            raise CodecUnavailable(
                f"{path} liefert {samples} Samples / {bits} Bit, "
                f"erwartet {SUBFRAME_SAMPLES} / {SUBFRAME_BITS}"
            )

    def reset(self) -> None:
        self.enc.tetra_enc_init()
        self.dec.tetra_dec_init()

    # ------------------------------------------------------------ Subframes

    def encode(self, pcm: bytes) -> int:
        """240 Samples (int16 little endian) -> 137-Bit-Integer."""
        if len(pcm) != SUBFRAME_SAMPLES * 2:
            raise ValueError(
                f"{SUBFRAME_SAMPLES * 2} Byte erwartet, {len(pcm)} bekommen"
            )
        buf = (ctypes.c_int16 * SUBFRAME_SAMPLES).from_buffer_copy(pcm)
        bits = (ctypes.c_int16 * SUBFRAME_BITS)()
        self.enc.tetra_enc(buf, bits)

        value = 0
        for bit in bits:
            value = (value << 1) | (bit & 1)
        return value

    def decode(self, subframe: int, bfi: int = 0) -> bytes:
        """137-Bit-Integer -> 240 Samples (int16 little endian)."""
        bits = (ctypes.c_int16 * SUBFRAME_BITS)()
        for i in range(SUBFRAME_BITS):
            bits[i] = (subframe >> (SUBFRAME_BITS - 1 - i)) & 1
        pcm = (ctypes.c_int16 * SUBFRAME_SAMPLES)()
        self.dec.tetra_dec(bits, ctypes.c_int16(bfi), pcm)
        return bytes(memoryview(pcm).cast("B"))

    # --------------------------------------------------------- ganze Frames

    def encode_frame(self, pcm: bytes) -> tuple[int, int]:
        """480 Samples (60 ms) -> die zwei Subframes fuer build_traffic_frame()."""
        if len(pcm) != FRAME_SAMPLES * 2:
            raise ValueError(
                f"{FRAME_SAMPLES * 2} Byte erwartet, {len(pcm)} bekommen"
            )
        half = SUBFRAME_SAMPLES * 2
        return self.encode(pcm[:half]), self.encode(pcm[half:])

    def decode_frame(self, sub1: int, sub2: int, bfi: int = 0) -> bytes:
        """Die zwei Subframes aus split_traffic_frame() -> 480 Samples."""
        return self.decode(sub1, bfi) + self.decode(sub2, bfi)
