# --------------------------------------------------------------- Codec-Stufe
# Holt den ETSI-Referenzcodec, patcht ihn und baut libtetraacelp_*.so.
# Der ETSI-Code landet nur hier, nie im Repo und nie im fertigen Image.

FROM debian:bookworm-slim AS codec

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev wget unzip patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY codec ./codec
RUN sh codec/build.sh /out


# ------------------------------------------------------------- svxlink-Stufe
# Basis ist DL1HRCs Zweig tetra-contrib (State-Events fuer TETRA durch den
# Reflektor). Der hat aber kein UsrpLogic - das kommt aus dem Zweig
# svxlink-usrp dazu, und svxlink/patches/01-usrplogic-tetra.patch verbindet
# beides: ISSI und SDS als Qso:info / Sds:info / ForwardSds:info.
#
# Beide Staende sind auf Commits festgenagelt. Ein neuerer Stand heisst:
# Hashes hier aendern und pruefen, ob die Patches noch greifen.
#
# Und Achtung: WITH_CONTRIB_USRP_LOGIC ist eine CMake-Option, die per
# Default AUS ist - ohne sie baut svxlink durch, aber ohne UsrpLogic.
#
# Dauert je nach Maschine 20-60 Minuten, wird aber gecached.

FROM debian:bookworm-slim AS svxlink

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config ca-certificates \
        libsigc++-2.0-dev libgsm1-dev libpopt-dev tcl-dev \
        libgcrypt20-dev libspeex-dev libasound2-dev libopus-dev \
        libcurl4-openssl-dev libjsoncpp-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ARG SVXLINK_TETRA_COMMIT=0d52873e0ec5b2c697be6074bc3a996f65c69b3f
ARG SVXLINK_USRP_COMMIT=a1e3427109c1eaa0f1f0f28ea7a63908a57bef8a

WORKDIR /build
COPY svxlink/patches /build/patches
RUN git init -q svxlink \
    && cd svxlink \
    && git remote add origin https://github.com/dl1hrc/svxlink.git \
    && git fetch -q --depth 1 origin "$SVXLINK_TETRA_COMMIT" "$SVXLINK_USRP_COMMIT" \
    && git checkout -q "$SVXLINK_TETRA_COMMIT" \
    && git checkout -q "$SVXLINK_USRP_COMMIT" -- src/svxlink/svxlink/contrib/UsrpLogic \
    && git apply --whitespace=nowarn /build/patches/*.patch

WORKDIR /build/svxlink/src/build
RUN cmake \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr \
        -DSYSCONF_INSTALL_DIR=/etc \
        -DLOCAL_STATE_DIR=/var \
        -DUSE_QT=NO \
        -DWITH_CONTRIB_USRP_LOGIC=ON \
        .. \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out

# Ohne UsrpLogic.so waere der ganze Build umsonst - lieber hier abbrechen
# als spaeter im Betrieb raetseln.
RUN find /out -name 'UsrpLogic*' -print | grep -q UsrpLogic.so \
    || (echo "UsrpLogic.so wurde nicht gebaut!" >&2; find /out -name '*.so' >&2; exit 1)


# ---------------------------------------------------------------- Laufzeit

# Bookworm festgenagelt: die svxlink- und Codec-Stufen bauen auf Bookworm,
# und python:3.12-slim ist inzwischen Debian 13. Gemischt passen die
# Bibliotheks-Sonames nicht zusammen (libjsoncpp25 vs 26).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SVXTETRA_CONFIG=/data/config.yaml \
    SVXTETRA_ACELP_DIR=/opt/dmo2tmo-svxbridge/lib

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsigc++-2.0-0v5 libgsm1 libpopt0 tcl libgcrypt20 \
        libspeex1 libasound2 libopus0 libcurl4 libjsoncpp25 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dmo2tmo-svxbridge

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=codec /out/libtetraacelp_enc.so /out/libtetraacelp_dec.so /opt/dmo2tmo-svxbridge/lib/
COPY --from=svxlink /out/ /

COPY app ./app
COPY selftest.py smoketest.py codectest.py livetest.py confgen.py manager.py \
     config.example.yaml ./

VOLUME ["/data"]
EXPOSE 3007

CMD ["python3", "manager.py"]
