"""Create the healthy etcher signal plot and wafer dataset tree."""

from __future__ import annotations

import math
import struct
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


def elements(data: bytes):
    pos = 0
    while pos + 8 <= len(data):
        first, second = struct.unpack_from("<II", data, pos)
        if first >> 16:
            kind, size = first & 0xFFFF, first >> 16
            yield kind, data[pos + 4 : pos + 4 + size]
            pos += 8
        else:
            kind, size = first, second
            yield kind, data[pos + 8 : pos + 8 + size]
            pos += 8 + ((size + 7) // 8) * 8


def matrix(data: bytes):
    parts = list(elements(data))
    flags = struct.unpack_from("<I", parts[0][1])[0]
    dims = struct.unpack("<" + "i" * (len(parts[1][1]) // 4), parts[1][1])
    name = parts[2][1].decode("ascii")
    cls = flags & 0xFF
    if cls == 1:  # Cell array, stored in MATLAB column-major order.
        value = [matrix(part) for kind, part in parts[3:] if kind == 14]
    elif cls == 2:  # Scalar structure containing a Data matrix.
        value = matrix(next(part for kind, part in reversed(parts) if kind == 14))
    elif cls == 4:  # Character array.
        value = parts[3][1].decode("utf-8", errors="replace")
    elif cls == 6:  # Numeric array (usually double; labels may be uint8).
        if parts[3][0] == 9:
            value = list(struct.unpack("<" + "d" * (len(parts[3][1]) // 8), parts[3][1]))
        else:
            value = list(parts[3][1])
    else:
        value = parts[3][1]
    return name, value, dims


def matlab_file(path: Path):
    raw = path.read_bytes()
    pos, result = 128, {}
    while pos + 8 <= len(raw):
        kind, size = struct.unpack_from("<II", raw, pos)
        payload = raw[pos + 8 : pos + 8 + size]
        if kind == 15:
            payload = zlib.decompress(payload)
            kind, size2 = struct.unpack_from("<II", payload)
            payload = payload[8 : 8 + size2]
        if kind == 14:
            name, value, dims = matrix(payload)
            result[name] = (value, dims)
        pos += 8 + size  # MATLAB's compressed elements here have no padding.
    return result


def workbook_bytes(xls: bytes):
    """Read the Workbook stream from this standard 512-byte-sector OLE file."""
    sector_size = 1 << struct.unpack_from("<H", xls, 30)[0]
    fat_sector = struct.unpack_from("<I", xls, 76)[0]
    fat_data = xls[512 + fat_sector * sector_size : 512 + (fat_sector + 1) * sector_size]
    fat = struct.unpack("<" + "I" * (sector_size // 4), fat_data)

    def chain(first):
        data = bytearray()
        seen = set()
        while first < len(fat) and first not in seen:
            seen.add(first)
            offset = 512 + first * sector_size
            data.extend(xls[offset : offset + sector_size])
            first = fat[first]
        return bytes(data)

    directory = chain(struct.unpack_from("<I", xls, 48)[0])
    for offset in range(0, len(directory), 128):
        entry = directory[offset : offset + 128]
        name = entry[: struct.unpack_from("<H", entry, 64)[0] - 2].decode("utf-16le")
        if name == "Workbook":
            start, size = struct.unpack_from("<IQ", entry, 116)
            return chain(start)[:size]
    raise ValueError("Workbook stream not found")


def biff_records(data: bytes, start=0):
    pos = start
    while pos + 4 <= len(data):
        kind, size = struct.unpack_from("<HH", data, pos)
        yield kind, data[pos + 4 : pos + 4 + size], pos
        pos += 4 + size


def ground_truth_rows():
    workbook = PROJECT_ROOT / "ground-truth/Grouth_Truth_Etcher.xls"
    if workbook.is_file():
        data = workbook_bytes(workbook.read_bytes())
    else:
        with zipfile.ZipFile(PROJECT_ROOT / "Project1_Etcher_GroundTruth.zip") as archive:
            name = next(n for n in archive.namelist() if n.endswith("Grouth_Truth_Etcher.xls"))
            data = workbook_bytes(archive.read(name))
    sheets = []
    for kind, payload, _ in biff_records(data):
        if kind == 0x0085:  # BOUNDSHEET
            offset = struct.unpack_from("<I", payload)[0]
            count = payload[6]
            title = payload[8 : 8 + count].decode("latin1", errors="replace")
            sheets.append((title, offset))
    result = {}
    for title, offset in sheets:
        cells = {}
        for kind, payload, _ in biff_records(data, offset):
            if kind == 0x000A:
                break
            if kind == 0x0203:  # NUMBER
                row, col = struct.unpack_from("<HH", payload)
                cells[row, col] = struct.unpack_from("<d", payload, 6)[0]
            elif kind == 0x027E:  # RK
                row, col = struct.unpack_from("<HH", payload)
                raw = struct.unpack_from("<I", payload, 6)[0]
                value = (raw >> 2) if raw & 2 else struct.unpack("<d", struct.pack("<Q", (raw & ~3) << 32))[0]
                cells[row, col] = value / 100 if raw & 1 else value
        result[title] = cells
    return result


def run_matrix(cell):
    _, value, _ = cell
    _, data, dims = value
    rows, cols = dims
    return [[data[r + c * rows] for c in range(cols)] for r in range(rows)]


def write_plot(data, names, title, output):
    width, height = 1700, 1280
    left, top, gap_x, gap_y = 70, 80, 25, 26
    columns, rows = 5, 4
    panel_w = (width - left - 45 - (columns - 1) * gap_x) / columns
    panel_h = (height - top - 65 - (rows - 1) * gap_y) / rows
    blue = "#1f77b4"  # Matplotlib's default blue.
    red = "#d62728"
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
              '<rect width="100%" height="100%" fill="#fff"/>',
              '<style>text{font-family:Arial,sans-serif;fill:#1d2730}.title{font-size:23px;font-weight:bold}.label{font-size:14px;font-weight:bold}.tick{font-size:10px;fill:#56616a}</style>',
              f'<text x="70" y="40" class="title">{escape(title)}</text>',
              f'<line x1="{width - 330}" y1="30" x2="{width - 290}" y2="30" stroke="{blue}" stroke-width="3"/>',
              f'<text x="{width - 280}" y="35">Fab step 4</text>',
              f'<line x1="{width - 170}" y1="30" x2="{width - 130}" y2="30" stroke="{red}" stroke-width="3"/>',
              f'<text x="{width - 120}" y="35">Fab step 5</text>']
    times = [r[0] - data[0][0] for r in data]
    all_t = [t for t in times if math.isfinite(t)]
    tmin, tmax = min(all_t), max(all_t)

    for panel, col in enumerate(range(1, 21)):
        row, grid_col = divmod(panel, columns)
        x0 = left + grid_col * (panel_w + gap_x)
        y0 = top + row * (panel_h + gap_y)
        px, py = x0 + 47, y0 + 26
        pw, ph = panel_w - 62, panel_h - 58
        values = [r[col] for r in data]
        finite = [v for v in values if math.isfinite(v)]
        vmin, vmax = min(finite), max(finite)
        if vmax == vmin:
            vmax += 0.5
            vmin -= 0.5
        else:
            padding = (vmax - vmin) * 0.05
            vmin -= padding
            vmax += padding
        pieces += [f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{panel_w:.1f}" height="{panel_h:.1f}" rx="5" fill="#fafbfd" stroke="#dce2e6"/>',
                   f'<text x="{x0 + 10:.1f}" y="{y0 + 18:.1f}" class="label">{escape(names[col])}</text>',
                   f'<line x1="{px:.1f}" y1="{py + ph:.1f}" x2="{px + pw:.1f}" y2="{py + ph:.1f}" stroke="#87939c"/>',
                   f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{px:.1f}" y2="{py + ph:.1f}" stroke="#87939c"/>',
                   f'<text x="{px - 5:.1f}" y="{py + 5:.1f}" text-anchor="end" class="tick">{vmax:.3g}</text>',
                   f'<text x="{px - 5:.1f}" y="{py + ph:.1f}" text-anchor="end" class="tick">{vmin:.3g}</text>',
                   f'<text x="{px:.1f}" y="{py + ph + 14:.1f}" class="tick">0</text>',
                   f'<text x="{px + pw:.1f}" y="{py + ph + 14:.1f}" text-anchor="end" class="tick">{tmax:.0f} s</text>']
        coords = []
        for t, v in zip(times, values):
            if math.isfinite(t) and math.isfinite(v):
                xx = px + (t - tmin) / max(tmax - tmin, 1) * pw
                yy = py + ph - (v - vmin) / (vmax - vmin) * ph
                coords.append(f"{xx:.1f},{yy:.1f}")
            else:
                coords.append(None)
        for i in range(1, len(coords)):
            if coords[i - 1] is not None and coords[i] is not None:
                color = blue if data[i][1] == 4 else red
                pieces.append(f'<polyline points="{coords[i - 1]} {coords[i]}" fill="none" stroke="{color}" stroke-width="1.65" stroke-linejoin="round" stroke-linecap="round"/>')
    pieces += [f'<text x="{width / 2:.0f}" y="{height - 20}" text-anchor="middle">Elapsed Time (seconds)</text>', '</svg>']
    output.write_text("\n".join(pieces), encoding="utf-8")


def draw_sensor_plot():
    base = matlab_file(PROJECT_ROOT / "training/Baseline1DataSet.mat")
    names = [item[1] for item in base["VarName"][0]]
    healthy = run_matrix(base["Baseline1Run"][0][0])

    write_plot(healthy, names, "Time-domain Sensor Signals from Healthy Run of Etcher 1", ROOT / "etcher1_healthy.svg")
    print(f"Created one 20-panel plot: {len(healthy)} healthy samples.")




def load_groups():
    truth = ground_truth_rows()
    groups = {}
    for etcher in (1, 2, 3):
        base = matlab_file(PROJECT_ROOT / f"training/Baseline{etcher}DataSet.mat")
        test = matlab_file(PROJECT_ROOT / f"testing/TestCase{etcher}DataSet.mat")
        train_count = len(base[f"Baseline{etcher}Run"][0])
        test_count = len(test[f"TestCase{etcher}Run"][0])
        train_ids = [int(truth[f"Baseline{etcher}"][6 + i, 3]) for i in range(train_count)]
        test_ids = [int(truth[f"Test{etcher}"][6 + i, 3]) for i in range(test_count)]
        # In the supplied ground-truth workbook, IDs 109–129 are faulty runs.
        groups[etcher] = {
            "Train": {"Healthy": train_ids, "Faulty": []},
            "Test": {
                "Healthy": [wafer for wafer in test_ids if wafer <= 108],
                "Faulty": [wafer for wafer in test_ids if wafer >= 109],
            },
        }
    return groups


BLUE, RED = "#2563a6", "#c83737"
OUTPUT = ROOT / "etcher_dataset_tree.svg"


def block(ax, x, y, width, height, title, face="#ffffff", edge="#cbd5e1"):
    ax.add_patch(FancyBboxPatch(
        (x, y), width, height,
        boxstyle="round,pad=0,rounding_size=12",
        facecolor=face, edgecolor=edge, linewidth=1.5,
    ))
    ax.text(x + width / 2, y + 29, title,
            ha="center", va="center", fontsize=12, weight="bold")


def connect(ax, start, end):
    x1, y1 = start
    x2, y2 = end
    middle = (y1 + y2) / 2
    ax.plot([x1, x1, x2, x2], [y1, middle, middle, y2],
            color="#94a3b8", linewidth=1.5, zorder=0)


def draw_dataset_tree():
    groups = load_groups()
    fig, ax = plt.subplots(figsize=(18, 5.6))
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)
    ax.set_xlim(0, 1800)
    ax.set_ylim(640, 100)
    ax.axis("off")

    for etcher, splits in groups.items():
        section_x = 50 + (etcher - 1) * 580
        etcher_x = section_x + 155
        block(ax, etcher_x, 120, 240, 62, f"Etcher {etcher}", "#e8f0fa", "#a9c5e4")
        for split_index, (split, conditions) in enumerate(splits.items()):
            split_x = section_x + split_index * 280 + 35
            connect(ax, (etcher_x + 120, 182), (split_x + 110, 245))
            block(ax, split_x, 245, 220, 62, split)
            for condition_index, (condition, ids) in enumerate(conditions.items()):
                leaf_x = section_x + split_index * 280 + condition_index * 135
                connect(ax, (split_x + 110, 307), (leaf_x + 62.5, 370))
                color = BLUE if condition == "Healthy" else RED
                face = "#eef5fc" if condition == "Healthy" else "#fdf0f0"
                block(ax, leaf_x, 370, 125, 240, condition, face, color)
                ax.text(leaf_x + 62.5, 423, f"{len(ids)} runs",
                        ha="center", va="center", fontsize=10, color="#64748b")
                if not ids:
                    ax.text(leaf_x + 62.5, 482, "No samples",
                            ha="center", va="center", fontsize=10, color="#64748b")
                for index, _wafer_id in enumerate(ids):
                    x = leaf_x + 19 + (index % 5) * 22
                    y = 453 + (index // 5) * 25
                    ax.scatter(x, y, s=70, color=color, zorder=2)

    fig.savefig(OUTPUT, transparent=True, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    print(f"Created {OUTPUT.name}")




if __name__ == "__main__":
    draw_sensor_plot()
    draw_dataset_tree()
