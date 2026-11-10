"""Map a parsed Song onto feedpak's JSON/YAML and write the package.

The interesting part is the difficulty ladder: Rocksmith stores one Level per
difficulty and slices the song into phrase iterations, each of which tops out at
some difficulty. To get the "full" chart we take, for every iteration, the notes
from the Level matching that iteration's max difficulty. That reproduces what the
game plays on the hardest setting and matches metadata.maxNotes exactly.
"""

import json
import shutil
import time
import zipfile
from pathlib import Path

from . import sng
from .audio import Tools
from .psarc import read as read_psarc
from .settings import Options, render_name

FEEDPAK_VERSION = "1.14.0"


class ConversionError(Exception):
    """Raised for problems the user can act on (bad input, missing tools)."""

# Rocksmith left-hand finger ids already line up with feedpak's (0=thumb..4=pinky).
_ARR_SORT = {"lead": 0, "combo": 1, "rhythm": 2, "bass": 3}

# note-mask bit -> feedpak note flag
_FLAGS = [
    ("ho", sng.HAMMERON), ("po", sng.PULLOFF), ("hm", sng.HARMONIC),
    ("hp", sng.PINCHHARMONIC), ("pm", sng.PALMMUTE), ("mt", sng.MUTE),
    ("tr", sng.TREMOLO), ("ac", sng.ACCENT), ("ln", sng.LINKNEXT),
    ("fhm", sng.FRETHANDMUTE), ("ig", sng.IGNORE),
]


_TUNING_NAMES = {
    (0, 0, 0, 0, 0, 0): "E Standard",
    (-1, -1, -1, -1, -1, -1): "Eb Standard",
    (-2, -2, -2, -2, -2, -2): "D Standard",
    (-3, -3, -3, -3, -3, -3): "C# Standard",
    (-4, -4, -4, -4, -4, -4): "C Standard",
    (-2, 0, 0, 0, 0, 0): "Drop D",
    (-3, -1, -1, -1, -1, -1): "Eb Drop Db",
    (-4, -2, -2, -2, -2, -2): "D Drop C",
    (-5, -3, -3, -3, -3, -3): "C# Drop B",
}


def _r(x):
    return round(float(x), 3)


def _tuning_name(offsets):
    t = tuple(int(x) for x in offsets)
    return _TUNING_NAMES.get(t) or "custom " + "/".join(str(x) for x in t)


def _size(n):
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(n, 1024) // 1024} KB"


def _mmss(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _flatten(song):
    """Return (notes, anchors, handshapes) for the max-difficulty chart."""
    phrase_maxdiff = [song.phrases[it.phraseId].maxDifficulty
                      for it in song.phraseIterations]
    iters = song.phraseIterations

    notes, anchors, shapes = [], [], []
    for level in song.levels:
        wanted = {i for i, md in enumerate(phrase_maxdiff) if md == level.difficulty}
        if not wanted:
            continue
        notes += [n for n in level.notes if n.iterId in wanted]
        anchors += [a for a in level.anchors if a.iterId in wanted]

        spans = [(iters[i].time, iters[i].endTime) for i in sorted(wanted)]
        for is_arp, prints in enumerate(level.fingerprints):
            for fp in prints:
                if any(a <= fp.startTime < b for a, b in spans):
                    shapes.append((fp, bool(is_arp)))

    notes.sort(key=lambda n: n.time)
    anchors.sort(key=lambda a: a.time)
    shapes.sort(key=lambda s: s[0].startTime)
    return notes, anchors, shapes


def _note(n):
    out = {"t": _r(n.time), "s": int(n.string), "f": int(n.fret)}
    if n.sustain > 0:
        out["sus"] = _r(n.sustain)
    if n.slideTo >= 0:
        out["sl"] = int(n.slideTo)
    if n.slideUnpitchTo >= 0:
        out["slu"] = int(n.slideUnpitchTo)
    if n.mask & sng.BEND and n.bends:
        out["bn"] = _r(max(b.step for b in n.bends))
        out["bnv"] = [{"t": _r(max(0.0, b.time - n.time)), "v": _r(b.step)}
                      for b in n.bends]
    for key, bit in _FLAGS:
        if n.mask & bit:
            out[key] = True
    if n.mask & sng.VIBRATO or n.vibrato:
        out["vb"] = True
    if n.mask & sng.TAP and n.tap > 0:
        out["tp"] = True
    if n.mask & sng.SLAP and n.slap > 0:
        out["slp"] = True
    if n.mask & sng.PLUCK and n.pluck > 0:
        out["plk"] = True
    if n.leftHand >= 0:
        out["fg"] = min(int(n.leftHand), 4)
    if n.pickDirection > 0:
        out["pkd"] = 1
    return out


def _chord(n, song):
    tmpl = song.chordTemplates[n.chordId]
    cn = None
    if (n.mask & sng.CHORDNOTES and n.chordNoteId != 0xFFFFFFFF
            and n.chordNoteId < len(song.chordNotes)):
        cn = song.chordNotes[n.chordNoteId]

    notes = []
    for s in range(6):
        if tmpl.frets[s] < 0:
            continue
        note = {"s": s, "f": int(tmpl.frets[s])}
        if n.sustain > 0:
            note["sus"] = _r(n.sustain)
        if cn is not None:
            if cn.slideTo[s] >= 0:
                note["sl"] = int(cn.slideTo[s])
            if cn.slideUnpitchTo[s] >= 0:
                note["slu"] = int(cn.slideUnpitchTo[s])
            if cn.vibrato[s]:
                note["vb"] = True
            for key, bit in (("ho", sng.HAMMERON), ("po", sng.PULLOFF),
                             ("pm", sng.PALMMUTE), ("mt", sng.MUTE),
                             ("fhm", sng.FRETHANDMUTE), ("ac", sng.ACCENT)):
                if cn.mask[s] & bit:
                    note[key] = True
            if cn.bends[s].count > 0:
                vals = cn.bends[s].steps[:cn.bends[s].count]
                note["bn"] = _r(max(b.step for b in vals))
                note["bnv"] = [{"t": _r(max(0.0, b.time - n.time)), "v": _r(b.step)}
                               for b in vals]
        notes.append(note)

    out = {"t": _r(n.time), "id": int(n.chordId), "notes": notes}
    if n.mask & sng.HIGHDENSITY:
        out["hd"] = True
    return out


def _phrases(song):
    """The per-phrase difficulty ladder (feedpak §6.7), or None if flat.

    Same shapes as the top-level arrays; the top-level chart stays the
    full-difficulty flatten so readers without a difficulty control are
    unaffected.
    """
    maxdiff = max((p.maxDifficulty for p in song.phrases), default=0)
    if len(song.levels) < 2 or maxdiff == 0:
        return None

    out = []
    for i, it in enumerate(song.phraseIterations):
        md = song.phrases[it.phraseId].maxDifficulty
        levels = []
        for level in song.levels:
            if level.difficulty > md:
                continue
            raw = sorted((n for n in level.notes if n.iterId == i),
                         key=lambda n: n.time)
            anchors = [a for a in level.anchors if a.iterId == i]
            shapes = []
            for is_arp, prints in enumerate(level.fingerprints):
                shapes += [(fp, bool(is_arp)) for fp in prints
                           if it.time <= fp.startTime < it.endTime]
            if not (raw or anchors or shapes):
                continue
            notes, chords = [], []
            for n in raw:
                (chords if n.mask & sng.CHORD else notes).append(
                    _chord(n, song) if n.mask & sng.CHORD else _note(n))
            levels.append({
                "difficulty": int(level.difficulty),
                "notes": notes,
                "chords": chords,
                "anchors": [{"time": _r(a.time), "fret": int(a.fret),
                             "width": int(a.width)} for a in anchors],
                "handshapes": [{"chord_id": int(fp.chordId),
                                "start_time": _r(fp.startTime),
                                "end_time": _r(fp.endTime), "arp": arp}
                               for fp, arp in sorted(shapes,
                                                     key=lambda s: s[0].startTime)],
            })
        out.append({"start_time": _r(it.time), "end_time": _r(it.endTime),
                    "max_difficulty": int(md), "levels": levels})
    return out if any(p["levels"] for p in out) else None


def _arrangement(song, name, *, include_phrases=True):
    notes_raw, anchors_raw, shapes_raw = _flatten(song)
    notes, chords = [], []
    for n in notes_raw:
        (chords if n.mask & sng.CHORD else notes).append(
            _chord(n, song) if n.mask & sng.CHORD else _note(n))
    out = {
        "name": name,
        "tuning": [int(x) for x in song.metadata.tuning],
        "capo": max(0, int(song.metadata.capo)),
        "notes": notes,
        "chords": chords,
        "anchors": [{"time": _r(a.time), "fret": int(a.fret), "width": int(a.width)}
                    for a in anchors_raw],
        "handshapes": [{"chord_id": int(fp.chordId), "start_time": _r(fp.startTime),
                        "end_time": _r(fp.endTime), "arp": arp}
                       for fp, arp in shapes_raw],
        "templates": [{"name": t.name,
                       "fingers": [int(x) for x in t.fingers],
                       "frets": [int(x) for x in t.frets],
                       "arp": bool(t.mask & sng.TEMPLATE_ARPEGGIO)}
                      for t in song.chordTemplates],
    }
    phrases = _phrases(song) if include_phrases else None
    if phrases:
        out["phrases"] = phrases
    return out


def _timeline(song):
    beats, measure = [], 0
    for b in song.beats:
        if b.beat == 0:
            measure += 1
            beats.append({"time": _r(b.time), "measure": measure})
        else:
            beats.append({"time": _r(b.time), "measure": -1})
    sections = [{"name": s.name, "number": int(s.number), "time": _r(s.startTime)}
                for s in song.sections]
    return {"version": 1, "beats": beats, "sections": sections}


def _tones(song, attrs):
    if not song.tones:
        return None
    letters = "ABCD"
    changes = []
    for t in song.tones:
        name = attrs.get(f"Tone_{letters[t.id]}") if t.id < 4 else None
        changes.append({"t": _r(t.time), "name": name or f"tone-{t.id}"})
    tones = {"changes": changes}
    if attrs.get("Tone_Base"):
        tones["base"] = attrs["Tone_Base"]
    return tones


def _lyrics(song):
    return [{"t": _r(v.time), "d": _r(v.length), "w": v.lyric} for v in song.vocals]


def _vocal_pitch(song):
    notes = [{"t": _r(v.time), "d": _r(v.length), "midi": int(v.note)}
             for v in song.vocals if 0 <= v.note < 128]
    return {"version": 1, "notes": notes} if notes else None


def _manifest_yaml(meta, arrangements, stems, extras):
    def q(s):
        return json.dumps(str(s), ensure_ascii=False)

    lines = [
        f'feedpak_version: "{FEEDPAK_VERSION}"',
        f"title: {q(meta['title'])}",
        f"artist: {q(meta['artist'])}",
    ]
    if meta.get("album"):
        lines.append(f"album: {q(meta['album'])}")
    if meta.get("year"):
        lines.append(f"year: {int(meta['year'])}")
    lines.append(f"duration: {round(meta['duration'], 3)}")

    lines.append("arrangements:")
    for a in arrangements:
        lines += [f"  - id: {a['id']}",
                  f"    name: {q(a['name'])}",
                  f"    file: {a['file']}",
                  f"    tuning: {a['tuning']}",
                  f"    capo: {a['capo']}",
                  f"    type: {a['type']}"]
        if a.get("centOffset"):
            lines.append(f"    centOffset: {a['centOffset']}")

    lines.append("stems:")
    for s in stems:
        lines += [f"  - id: {s['id']}",
                  f"    file: {s['file']}",
                  f"    default: {'true' if s.get('default') else 'false'}"]

    for key, val in extras.items():
        lines.append(f"{key}: {val}")
    return "\n".join(lines) + "\n"


def _identify(files):
    """Sort the extracted files into songs / manifest attributes / audio / art."""
    songs, attrs = {}, {}
    for name, blob in files.items():
        if name.endswith(".sng"):
            songs[Path(name).stem] = sng.Song.parse(blob)
        elif "/manifests/" in f"/{name}" and name.endswith(".json"):
            entries = json.loads(blob.decode("utf-8")).get("Entries", {})
            for entry in entries.values():
                if entry.get("Attributes"):
                    attrs[Path(name).stem] = entry["Attributes"]
    # Skip empty wems: some packs ship a 0-byte preview, which would fail the
    # whole conversion instead of just leaving the song without a preview.
    wems = sorted((blob for n, blob in files.items() if n.endswith(".wem") and blob),
                  key=len, reverse=True)
    dds = [files[n] for n in sorted(files, reverse=True) if n.endswith(".dds")]
    return songs, attrs, wems, dds


def convert_song(song_key, songs, attrs, wems, dds, out_path, out_dir, *,
                 keep_dir=False, options=None, log=print):
    """Convert a single song to feedpak format."""
    t0 = time.perf_counter()
    opt = options or Options()

    playable, vocals, meta_attrs = [], None, None
    for stem, song in songs.items():
        a = attrs.get(stem, {})
        name = a.get("ArrangementName") or stem.rsplit("_", 1)[-1].title()
        if name.lower() == "vocals" or stem.endswith("_vocals"):
            vocals = song
            continue
        playable.append((stem, song, a, name))
        if meta_attrs is None or name.lower() == "lead":
            meta_attrs = a
    if not playable:
        raise ConversionError("no playable arrangements in this psarc")
    playable.sort(key=lambda p: _ARR_SORT.get(p[3].lower(), 9))
    meta_attrs = meta_attrs or {}

    meta = {
        "title": meta_attrs.get("SongName", song_key),
        "artist": meta_attrs.get("ArtistName", "Unknown Artist"),
        "album": meta_attrs.get("AlbumName"),
        "year": meta_attrs.get("SongYear"),
        "duration": float(meta_attrs.get("SongLength")
                          or playable[0][1].metadata.songLength),
    }
    byline = f'"{meta["title"]}" by {meta["artist"]}'
    if meta.get("album"):
        byline += f" [{meta['album']}]"
    log(f"Converting {byline} ({_mmss(meta['duration'])})")

    if out_path is None:
        try:
            raw = render_name(opt.name_template,
                              artist=meta["artist"], title=meta["title"],
                              album=meta.get("album") or "",
                              year=meta.get("year") or "")
        except ValueError:
            raise ConversionError(
                f"bad filename template: {opt.name_template!r}")
        raw = raw or f"{meta['artist']} - {meta['title']}"
        safe = "".join(c if c.isalnum() or c in " -_'()." else "_"
                       for c in raw).strip(" .")
        out_path = out_dir / f"{safe}.feedpak"
    out_path = Path(out_path)

    if not opt.overwrite and out_path.exists():
        log(f"Skipped: {out_path.name} already exists")
        return out_path

    build = Path(str(out_path) + ".build")
    if build.exists():
        shutil.rmtree(build)
    (build / "arrangements").mkdir(parents=True)
    (build / "stems").mkdir()
    scratch = build / "_tmp"
    scratch.mkdir()

    def dump(obj, rel):
        with open(build / rel, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))

    manifest_arrs, timeline, extras = [], None, {}
    seen_ids = {}
    for stem, song, a, name in playable:
        # CDLC can carry several arrangements with the same name (two "Lead"
        # charts, say); keep them all instead of overwriting one JSON file.
        arr_id = name.lower().replace(" ", "_")
        seen_ids[arr_id] = seen_ids.get(arr_id, 0) + 1
        if seen_ids[arr_id] > 1:
            name = f"{name} {seen_ids[arr_id]}"
            arr_id = f"{arr_id}_{seen_ids[arr_id]}"
        arr = _arrangement(song, name, include_phrases=opt.include_phrases)
        tones = _tones(song, a)
        if tones:
            arr["tones"] = tones
        dump(arr, f"arrangements/{arr_id}.json")

        tuning = a.get("Tuning")
        entry = {
            "id": arr_id, "name": name, "file": f"arrangements/{arr_id}.json",
            "tuning": ([tuning[f"string{i}"] for i in range(6)] if tuning
                       else arr["tuning"]),
            "capo": int(a.get("CapoFret") or arr["capo"]),
            "type": "bass" if "bass" in arr_id else "guitar",
        }
        if float(a.get("CentOffset") or 0):
            entry["centOffset"] = float(a["CentOffset"])
        manifest_arrs.append(entry)
        if timeline is None:
            timeline = _timeline(song)
        tune = _tuning_name(entry["tuning"])
        if entry["capo"]:
            tune += f", capo {entry['capo']}"
        counts = f"{len(arr['notes'])} notes"
        if arr["chords"]:
            counts += f" + {len(arr['chords'])} chords"
        if "phrases" in arr:
            counts += f", {len(song.levels)} difficulty levels"
        log(f"  {arr_id:<9} {tune:<14} {counts}")

    dump(timeline, "song_timeline.json")
    extras["song_timeline"] = "song_timeline.json"

    if vocals is not None and vocals.vocals:
        dump(_lyrics(vocals), "lyrics.json")
        extras["lyrics"] = "lyrics.json"
        pitch = _vocal_pitch(vocals)
        if pitch:
            dump(pitch, "vocal_pitch.json")
            extras["vocal_pitch"] = "vocal_pitch.json"
        log(f"  lyrics    {len(vocals.vocals)} syllables"
            + (" with pitch" if pitch else ""))
    else:
        log("  lyrics    none in this chart")

    tools = Tools()
    stems = []
    if wems and not opt.include_audio:
        log("  audio     skipped (off in settings)")
    elif wems:
        if not tools.can_audio:
            raise ConversionError("need " + " and ".join(tools.missing())
                                  + " for audio (put vgmstream-cli in tools/vgmstream/)")
        log(f"  audio     encoding {_size(len(wems[0]))} wem...")
        ogg = build / "stems" / "full.ogg"
        tools.wem_to_ogg(wems[0], ogg, scratch, quality=opt.audio_quality)
        stems.append({"id": "full", "file": "stems/full.ogg", "default": True})
        audio_line = f"  audio     full.ogg ({_size(ogg.stat().st_size)})"
        if len(wems) > 1:
            tools.wem_to_ogg(wems[1], build / "preview.ogg", scratch,
                             quality=opt.audio_quality)
            extras["preview"] = "preview.ogg"
            audio_line += " + preview"
        log(audio_line)
    else:
        log("  warning: no .wem audio found")

    if dds and not opt.include_cover:
        log("  cover     skipped (off in settings)")
    elif dds and tools.ffmpeg:
        try:
            tools.dds_to_png(dds[0], build / "cover.png", scratch)
            extras["cover"] = "cover.png"
            log(f"  cover     cover.png ({_size((build / 'cover.png').stat().st_size)})")
        except RuntimeError as e:
            log(f"  warning: cover failed ({e})")

    extras["x_converted_from"] = "rocksmith2014-psarc"
    (build / "manifest.yaml").write_text(
        _manifest_yaml(meta, manifest_arrs, stems, extras), encoding="utf-8")
    shutil.rmtree(scratch)

    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(build.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(build).as_posix())

    if keep_dir or opt.keep_dir:
        final = out_path.with_name(out_path.name + ".dir")
        if final.exists():
            shutil.rmtree(final)
        build.rename(final)
    else:
        shutil.rmtree(build)

    log(f"Wrote {out_path} ({_size(out_path.stat().st_size)}, "
        f"{time.perf_counter() - t0:.1f}s)")
    return out_path


def _bank_wems(files, bank_name):
    """Wems referenced by a Wwise soundbank, largest first.

    Banks embed each streamed wem's id as a little-endian uint32; the wems
    themselves are stored as <id>.wem, so a byte search is enough to match them.
    """
    if not bank_name:
        return []
    bank = next((blob for n, blob in files.items()
                 if n.endswith(".bnk") and Path(n).name == bank_name), None)
    if bank is None:
        return []
    refs = [blob for n, blob in files.items()
            if n.endswith(".wem") and blob and Path(n).stem.isdigit()
            and int(Path(n).stem).to_bytes(4, "little") in bank]
    return sorted(refs, key=len, reverse=True)


def _album_art(files, urn):
    """DDS blobs for an AlbumArt urn like urn:image:dds:album_foo, largest first."""
    if not urn:
        return []
    key = urn.rsplit(":", 1)[-1]
    art = [blob for n, blob in files.items()
           if n.endswith(".dds") and Path(n).stem.startswith(key)]
    return sorted(art, key=len, reverse=True)


def convert(psarc_path, out_path=None, *, out_dir=None, keep_dir=False,
            options=None, log=print):
    """Convert every song in the psarc; returns the list of written paths."""
    psarc_path = Path(psarc_path)
    log(f"Reading {psarc_path.name}")
    files = read_psarc(psarc_path)
    songs, attrs, wems, dds = _identify(files)

    # Group arrangements by song: "foo_lead"/"foo_rhythm"/"foo_vocals" -> "foo".
    # Single-song files have one group; compilation discs have one per song.
    groups = {}
    for stem, song in songs.items():
        groups.setdefault(stem.rsplit("_", 1)[0], {})[stem] = song
    log(f"  found {len(groups)} song{'s' if len(groups) != 1 else ''}, "
        f"{len(songs)} arrangement{'s' if len(songs) != 1 else ''}")

    if out_path is not None and len(groups) > 1:
        raise ConversionError("cannot write multiple songs to a single output path")
    out_dir = Path(out_dir) if out_dir is not None else psarc_path.parent

    written = []
    for key, group in sorted(groups.items()):
        group_attrs = {s: attrs[s] for s in group if s in attrs}
        playable = [s for s in group
                    if not s.endswith("_vocals")
                    and (group_attrs.get(s, {}).get("ArrangementName")
                         or "").lower() != "vocals"]
        if not playable:
            log(f"  {key}: no playable arrangements, skipping")
            continue

        # Each song carries its own audio and art; fall back to the size-sorted
        # archive-wide lists when the manifest doesn't say (or banks are absent).
        sa = next((group_attrs[s] for s in playable if s in group_attrs), {})
        main = _bank_wems(files, sa.get("SongBank"))
        preview = _bank_wems(files, sa.get("PreviewBankPath"))
        song_wems = main[:1] + preview[:1] if main else wems
        song_dds = _album_art(files, sa.get("AlbumArt")) or dds

        written.append(convert_song(key, group, group_attrs, song_wems, song_dds,
                                    out_path, out_dir, keep_dir=keep_dir,
                                    options=options, log=log))

    if not written:
        raise ConversionError("no playable arrangements in this psarc")
    return written
