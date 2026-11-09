/*
 * Duenner Wrapper um den ETSI-Referenzcodec (EN 300 395-2), damit er sich
 * per ctypes ansprechen laesst.
 *
 * Die ETSI-Mains (scoder.c / sdecoder.c) sind Datei-I/O-Programme und werden
 * nicht mitgebaut. Wir rufen dieselben Routinen direkt auf, die sie aufrufen.
 *
 * Eine Einheit ist ein 30-ms-Subframe: 240 PCM-Samples <-> 137 Bit. Ein
 * Brew-Traffic-Frame sind zwei davon (60 ms).
 *
 * WARUM ZWEI BIBLIOTHEKEN:
 * scod_tet.c und sdec_tet.c definieren beide 'last_ener_pit'/'last_ener_cod',
 * und sub_sc_d.c greift per extern darauf zu - im Encoder- wie im
 * Decoder-Pfad. ETSI baut daraus zwei getrennte Programme, jedes loest den
 * extern gegen seine eigene Definition auf. Wuerden wir beides in eine .so
 * linken, teilten sich Encoder und Decoder diesen Zustand: es sind die
 * Vorframe-Energien der praediktiven Gain-Quantisierung, und eine Bruecke
 * codiert und decodiert gleichzeitig. Darum wird diese Datei zweimal
 * uebersetzt, einmal mit -DTETRA_ENCODER und einmal mit -DTETRA_DECODER.
 */

#include <stdint.h>
#include <string.h>

#include "source.h"

#define L_FRAME      240   /* PCM-Samples je 30-ms-Subframe */
#define SERIAL_SIZE  138   /* BFI + 137 Bit                 */
#define CODEC_BITS   137
#define ANA_SIZE      23   /* Analyseparameter (Encoder)    */
#define PRM_SIZE      24   /* BFI + 23 Parameter (Decoder)  */

#ifdef TETRA_ENCODER

/* Wird von Init_Coder_Tetra() gesetzt und zeigt in den internen Puffer. */
extern Word16 *new_speech;

void tetra_enc_init(void)
{
    Init_Pre_Process();
    Init_Coder_Tetra();
}

/*
 * pcm      : 240 Samples, 16 Bit signed
 * bits_out : 137 Woerter, je 0 oder 1, MSB zuerst
 */
void tetra_enc(const int16_t *pcm, int16_t *bits_out)
{
    Word16 ana[ANA_SIZE];
    Word16 syn[L_FRAME];
    Word16 serial[SERIAL_SIZE];

    memcpy(new_speech, pcm, L_FRAME * sizeof(Word16));

    Pre_Process(new_speech, (Word16)L_FRAME);
    Coder_Tetra(ana, syn);          /* syn = lokale Synthese, brauchen wir nicht */
    Prm2bits_Tetra(ana, serial);

    /* serial[0] ist das BFI-Feld und immer 0 - nur die 137 Nutzbits raus. */
    memcpy(bits_out, serial + 1, CODEC_BITS * sizeof(Word16));
}

#endif /* TETRA_ENCODER */

#ifdef TETRA_DECODER

void tetra_dec_init(void)
{
    Init_Decod_Tetra();
}

/*
 * bits_in : 137 Woerter, je 0 oder 1, MSB zuerst
 * bfi     : 0 = Frame gueltig, 1 = kaputt (dann interpoliert der Decoder)
 * pcm_out : 240 Samples, 16 Bit signed
 */
void tetra_dec(const int16_t *bits_in, int16_t bfi, int16_t *pcm_out)
{
    Word16 parm[PRM_SIZE];
    Word16 serial[SERIAL_SIZE];
    Word16 synth[L_FRAME];

    serial[0] = bfi;
    memcpy(serial + 1, bits_in, CODEC_BITS * sizeof(Word16));

    Bits2prm_Tetra(serial, parm);
    Decod_Tetra(parm, synth);
    Post_Process(synth, (Word16)L_FRAME);

    memcpy(pcm_out, synth, L_FRAME * sizeof(Word16));
}

#endif /* TETRA_DECODER */

/* Damit die Python-Seite pruefen kann, ob die .so zum Wrapper passt. */
int tetra_codec_frame_samples(void) { return L_FRAME; }
int tetra_codec_frame_bits(void)    { return CODEC_BITS; }
