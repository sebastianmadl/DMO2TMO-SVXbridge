#!/bin/sh
# Baut den TETRA-ACELP-Source-Codec als libtetraacelp.so.
#
# Der ETSI-Referenzcode darf aus Lizenzgruenden nicht ins Repo (Grundregel 1),
# also wird er hier zur Buildzeit von der ETSI-Seite geholt - genau so wie es
# osmo-tetra macht. Im Repo liegen nur die osmocom-Patches (GPL) und der
# Wrapper.
#
# Gebaut wird ausschliesslich der *Source*-Codec. Channel-Coding
# (ccod_tet/cdec_tet/sub_cc/sub_cd) lassen wir weg - das macht die
# Basisstation.
#
# Aufruf:  ./build.sh [ausgabeverzeichnis]

set -e

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE}
WORK=$HERE/build
SRC=$WORK/c-code

URL=https://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip
MD5_EXP=a8115fe68ef8f8cc466f4192572a1e3e
ZIP=$HERE/etsi_tetra_codec.zip

# --------------------------------------------------------------- Herunterladen

if [ ! -f "$ZIP" ]; then
    echo "Lade ETSI-Referenzcodec ..."
    if command -v wget >/dev/null 2>&1; then
        wget -q -O "$ZIP" "$URL"
    else
        curl -fsSL -o "$ZIP" "$URL"
    fi
else
    echo "ZIP ist schon da, kein Download noetig."
fi

echo "Pruefe MD5 ..."
MD5=$(md5sum "$ZIP" | awk '{ print $1 }')
if [ "$MD5" != "$MD5_EXP" ]; then
    echo "MD5 passt nicht: $MD5 statt $MD5_EXP" >&2
    echo "ETSI hat die Datei geaendert - Patches pruefen, bevor du weitermachst." >&2
    exit 1
fi

# ------------------------------------------------------------------- Entpacken

echo "Entpacke ..."
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"
# -L ist Pflicht: das ZIP hat GROSSGESCHRIEBENE Namen, die Patches und die
# #include-Zeilen erwarten sie klein.
unzip -q -L "$ZIP"

# --------------------------------------------------------------------- Patchen
#
# Die Patches sind gegen 'etsi/c-code/...' gebaut, deshalb -p1 aus $WORK heraus.

echo "Wende Patches an ..."
while read -r p; do
    [ -n "$p" ] || continue
    echo "  -> $p"
    patch -s -p1 < "$HERE/patches/$p"
done < "$HERE/patches/series"

# ------------------------------------------------------------------ Compilieren
#
# Dateiliste aus dem ETSI-Makefile: SRCS1 (scoder) und SRCS3 (sdecoder),
# jeweils ohne die main()-Programme scoder.c / sdecoder.c.
#
# Encoder und Decoder werden getrennt gelinkt. Grund steht ausfuehrlich in
# tetra_wrapper.c: scod_tet.c und sdec_tet.c definieren beide
# last_ener_pit/last_ener_cod, auf die sub_sc_d.c per extern zugreift. In
# einer gemeinsamen .so wuerden sich beide Richtungen den Praediktorzustand
# der Gain-Quantisierung teilen. ETSI baut aus demselben Grund zwei Programme.
#
# -Wl,-Bsymbolic sorgt dafuer, dass jede Bibliothek ihre eigenen Symbole
# benutzt, auch wenn beide gleichzeitig geladen sind.

COMMON="$SRC/sub_sc_d.c $SRC/sub_dsp.c $SRC/fbas_tet.c $SRC/fexp_tet.c \
        $SRC/fmat_tet.c $SRC/tetra_op.c"

mkdir -p "$OUT"

echo "Compiliere libtetraacelp_enc.so ..."
# shellcheck disable=SC2086
${CC:-gcc} -O2 -fPIC -shared -Wl,-Bsymbolic \
    -DTETRA_ENCODER \
    -I"$SRC" -I"$HERE" \
    "$SRC/scod_tet.c" $COMMON "$HERE/tetra_wrapper.c" \
    -o "$OUT/libtetraacelp_enc.so"

echo "Compiliere libtetraacelp_dec.so ..."
# shellcheck disable=SC2086
${CC:-gcc} -O2 -fPIC -shared -Wl,-Bsymbolic \
    -DTETRA_DECODER \
    -I"$SRC" -I"$HERE" \
    "$SRC/sdec_tet.c" $COMMON "$HERE/tetra_wrapper.c" \
    -o "$OUT/libtetraacelp_dec.so"

echo "Fertig: $OUT/libtetraacelp_enc.so + libtetraacelp_dec.so"
