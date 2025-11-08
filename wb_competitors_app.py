# wb_competitors_app.py
# Публичная версия: сохраняет фото конкурентов WB, делает Excel с картинками,
# коллаж и даёт скачать всё одним ZIP прямо из браузера.

import re
import io
import os
import json
import time
import shutil
import zipfile
import pathlib
import requests
import streamlit as st
import pandas as pd
from PIL import Image
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# ---------------- Streamlit page ----------------
st.set_page_config(page_title="WB Competitors Saver", page_icon="📦", layout="wide")
st.title("📦 WB Competitors Saver — публичная версия")

st.caption(
    "1) Вставь ссылки WB (каждая с новой строки). "
    "2) Нажми «Скачать фото» — создастся сессия с подпапками по артикулам. "
    "3) Нажми «Сформировать таблицу» — Excel с фото в ячейках и коллаж. "
    "4) Скачай ZIP всех файлов кнопкой ниже."
)

# ---------------- Константы ----------------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
}
TIMEOUT = 25
DEFAULT_SLIDES = 10           # если WB не сообщает pics
THUMB = (360, 360)            # превью в коллаже
CELL_PX = (160, 160)          # размер картинки в excel-ячейке (ширина, высота)

# Рабочая директория сессии (в облаке пишем в cwd)
def get_session_root(name_hint: str | None = None) -> pathlib.Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root_name = name_hint.strip() if name_hint and name_hint.strip() else f"WB_Save_{ts}"
    root = pathlib.Path.cwd() / root_name
    root.mkdir(parents=True, exist_ok=True)
    return root

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

# ---------------- WB utils ----------------
def extract_nm_id(url: str) -> str | None:
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        if "nm" in q and q["nm"]:
            return re.sub(r"\D", "", q["nm"][0])
        m = re.search(r"/catalog/(\d+)", u.path)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def fetch_card_json(nm: str) -> dict | None:
    api = (f"https://card.wb.ru/cards/v2/detail"
           f"?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}")
    r = requests.get(api, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    prods = data.get("data", {}).get("products", [])
    return prods[0] if prods else None

def parse_basics(prod: dict) -> tuple[str | None, str | None, int]:
    if not prod:
        return None, None, 0
    title = prod.get("name")
    brand = prod.get("brand")
    pics = int(prod.get("pics") or 0)
    if pics == 0:
        photos = (prod.get("media") or {}).get("photos") or []
        pics = len(photos)
    return title, brand, pics

def candidate_image_urls(nm_id: int, idx: int) -> list[str]:
    vol = nm_id // 100000
    part = nm_id // 1000
    baskets = [f"https://basket-{i:02d}.wb.ru" for i in range(1, 33)]
    baskets += [f"https://basket-{i:02d}.wbbasket.ru" for i in range(1, 33)]
    urls = []
    for host in baskets:
        base = f"{host}/vol{vol}/part{part}/{nm_id}/images/big/{idx}"
        urls.append(base + ".jpg")
        urls.append(base + ".webp")
    return urls

def try_download(urls: list[str], dest_path_stub: pathlib.Path) -> pathlib.Path | None:
    for u in urls:
        try:
            with requests.get(u, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
                if r.status_code == 200 and int(r.headers.get("Content-Length", "1")) > 0:
                    ext = ".jpg" if u.lower().endswith(".jpg") else ".webp"
                    final = dest_path_stub.with_suffix(ext)
                    with open(final, "wb") as f:
                        shutil.copyfileobj(r.raw, f)
                    return final
        except Exception:
            pass
    return None

# ---------------- Slides counting ----------------
def detect_max_slides(root: pathlib.Path) -> int:
    max_slides = 0
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        imgs = list(sub.glob("*.jpg")) + list(sub.glob("*.webp"))
        if not imgs:
            continue
        local_max = 0
        for p in imgs:
            try:
                local_max = max(local_max, int(p.stem))
            except Exception:
                pass
        if local_max == 0:
            local_max = len(imgs)
        max_slides = max(max_slides, local_max)
    return max_slides or 1

# ---------------- Excel with images ----------------
def _image_to_png_bytes(path: pathlib.Path, max_w: int, max_h: int) -> BytesIO | None:
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((max_w, max_h))
        bio = BytesIO()
        im.save(bio, format="PNG", optimize=True)
        bio.seek(0)
        return bio
    except Exception:
        return None

def save_excel_with_images(root: pathlib.Path,
                           summary_rows: list[dict],
                           limit_slides: int = 10,
                           cell_w_px: int = 160,
                           cell_h_px: int = 160) -> pathlib.Path:
    out = root / "listing_matrix.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        # Сводка
        df_sum = pd.DataFrame(summary_rows)
        if not df_sum.empty:
            cols = ["order", "nm_id", "brand", "title", "slides", "folder"]
            df_sum = df_sum[[c for c in cols if c in df_sum.columns]]
        df_sum.to_excel(writer, sheet_name="Сводка", index=False)

        # Матрица с изображениями
        wb = writer.book
        ws = wb.add_worksheet("Матрица")

        competitors = sorted([p for p in root.iterdir() if p.is_dir()])
        nm_ids = [c.name.split("_")[-1] for c in competitors]

        header_fmt = wb.add_format({"bold": True, "align": "center"})
        ws.write(0, 0, "")
        for col, nm in enumerate(nm_ids, start=1):
            ws.write(0, col, nm, header_fmt)

        row_labels_fmt = wb.add_format({"align": "center"})
        for r in range(1, limit_slides + 1):
            ws.write(r, 0, f"{r} слайд", row_labels_fmt)

        col_width_chars = max(12, int(cell_w_px / 7))
        row_height_pts = max(24, int(cell_h_px / 1.33))
        ws.set_column(0, 0, 12)
        for c in range(1, len(nm_ids) + 1):
            ws.set_column(c, c, col_width_chars)
        for r in range(1, limit_slides + 1):
            ws.set_row(r, row_height_pts)

        x_offset = 5
        y_offset = 5

        for col, comp_dir in enumerate(competitors, start=1):
            imgs = sorted(list(comp_dir.glob("*.jpg")) + list(comp_dir.glob("*.webp")),
                          key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
            for r_idx in range(limit_slides):
                if r_idx < len(imgs):
                    bio = _image_to_png_bytes(imgs[r_idx], cell_w_px, cell_h_px)
                    if bio:
                        ws.insert_image(r_idx + 1, col, imgs[r_idx].name,
                                        {"image_data": bio, "x_offset": x_offset, "y_offset": y_offset})
    return out

# ---------------- Collage ----------------
def save_collage(root: pathlib.Path, limit_slides: int = DEFAULT_SLIDES) -> pathlib.Path | None:
    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    if not competitors:
        return None
    grid = []
    max_rows = 0
    for c in competitors:
        imgs = sorted(list(c.glob("*.jpg")) + list(c.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        imgs = imgs[:limit_slides]
        max_rows = max(max_rows, len(imgs))
        grid.append(imgs)
    if max_rows == 0:
        return None
    cols = len(grid)
    rows = max_rows
    cell_w, cell_h = THUMB
    pad = 10
    W = cols * cell_w + (cols + 1) * pad
    H = rows * cell_h + (rows + 1) * pad
    canvas = Image.new("RGB", (W, H), (245, 245, 245))
    for x, col_imgs in enumerate(grid):
        for y in range(rows):
            if y < len(col_imgs):
                try:
                    img = Image.open(col_imgs[y]).convert("RGB")
                    img.thumbnail(THUMB)
                    ox = pad + x * (cell_w + pad) + (cell_w - img.width)//2
                    oy = pad + y * (cell_h + pad) + (cell_h - img.height)//2
                    canvas.paste(img, (ox, oy))
                except Exception:
                    pass
    out = root / "matrix_preview.jpg"
    canvas.save(out, format="JPEG", quality=85)
    return out

# ---------------- ZIP (скачивание одним файлом) ----------------
def make_zip_bytes(root: pathlib.Path) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in root.rglob("*"):
            if path.is_file():
                # внутри ZIP делаем относительные пути
                z.write(path, arcname=str(path.relative_to(root)))
    mem.seek(0)
    return mem.read()

# ---------------- UI ----------------
with st.form("form_links"):
    urls_text = st.text_area("Ссылки на товары WB (по одной на строке)", height=160)
    session_name = st.text_input("Имя общей папки (необязательно)", placeholder="Анализ_товаров")
    c1, c2 = st.columns(2)
    with c1:
        do_download = st.form_submit_button("⬇️ Скачать фото")
    with c2:
        do_table = st.form_submit_button("📊 Сформировать таблицу (Excel + коллаж)")

# Управляем текущим корнем сессии через session_state
if "root_path" not in st.session_state and (do_download or do_table):
    st.session_state["root_path"] = str(get_session_root(session_name))

root = pathlib.Path(st.session_state.get("root_path", "")) if (do_download or do_table) else None

def parse_input_urls(text: str) -> list[str]:
    return [u.strip() for u in (text or "").splitlines() if u.strip()]

# --- Кнопка 1: Скачать фото ---
if do_download:
    if root is None:
        st.stop()
    links = parse_input_urls(urls_text)
    if not links:
        st.error("Добавь хотя бы одну ссылку.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()
    ok_list, err_list = [], []
    total = len(links)

    for idx, url in enumerate(links, start=1):
        status.write(f"Обработка {idx}/{total}: {url}")
        nm_raw = extract_nm_id(url)
        if not nm_raw:
            err_list.append((url, "Не найден артикул (nm_id)"))
            progress.progress(idx/total); continue

        nm = int(nm_raw)
        try:
            prod = fetch_card_json(nm_raw)
        except Exception as e:
            err_list.append((url, f"API ошибка: {e}"))
            progress.progress(idx/total); continue

        title, brand, pics = parse_basics(prod)
        if pics <= 0:
            pics = DEFAULT_SLIDES

        subdir = root / f"{idx:03d}_{nm}"
        ensure_dir(subdir)

        meta = {"url": url, "nm_id": nm, "title": title, "brand": brand, "saved_at": datetime.now().isoformat()}
        (subdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        saved = 0
        for i in range(1, pics + 1):
            dest_stub = subdir / f"{i}"
            final = try_download(candidate_image_urls(nm, i), dest_stub)
            if final:
                saved += 1

        if saved > 0:
            ok_list.append((url, subdir.name, saved))
        else:
            err_list.append((url, "Не удалось сохранить изображения"))

        progress.progress(idx/total)
        time.sleep(0.03)

    st.success(f"Готово! Папка сессии: {root}")
    if ok_list:
        st.subheader("✅ Сохранены")
        for url, folder, cnt in ok_list:
            st.write(f"- {folder} — {cnt} фото — {url}")
    if err_list:
        st.subheader("⚠️ Ошибки")
        for url, msg in err_list:
            st.write(f"- {url}: {msg}")

    # Кнопка скачать ZIP сразу после скачивания
    zip_bytes = make_zip_bytes(root)
    st.download_button("⬇️ Скачать ZIP сессии", data=zip_bytes,
                       file_name=f"{root.name}.zip", mime="application/zip")

# --- Кнопка 2: Сформировать таблицу ---
if do_table:
    if root is None or not root.exists():
        st.error("Папка сессии не найдена. Сначала нажми «Скачать фото».")
        st.stop()

    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    if not competitors:
        st.error("Нет скачанных папок конкурентов.")
        st.stop()

    # Сводка
    summary_rows = []
    for sub in competitors:
        nm = sub.name.split("_")[-1]
        imgs = sorted(list(sub.glob("*.jpg")) + list(sub.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        meta = sub / "meta.json"
        title = brand = None
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                title, brand = m.get("title"), m.get("brand")
            except Exception:
                pass
        summary_rows.append({
            "order": sub.name.split("_")[0],
            "nm_id": nm,
            "brand": brand,
            "title": title,
            "slides": len(imgs),
            "folder": sub.name
        })

    # Реальное число слайдов для таблицы
    max_slides = detect_max_slides(root)

    xlsx_path = save_excel_with_images(root, summary_rows, limit_slides=max_slides,
                                       cell_w_px=CELL_PX[0], cell_h_px=CELL_PX[1])
    collage_path = save_collage(root, min(max_slides, DEFAULT_SLIDES))

    st.success("Готово! Таблица и коллаж созданы.")
    st.write(f"📁 Папка сессии: {root}")
    st.write(f"📊 Excel: {xlsx_path.name}")
    if collage_path:
        st.write(f"🖼 Коллаж: {collage_path.name}")
        st.image(str(collage_path), caption="Matrix Preview", use_container_width=True)

    # Кнопка скачать ZIP полностью
    zip_bytes = make_zip_bytes(root)
    st.download_button("⬇️ Скачать ZIP сессии", data=zip_bytes,
                       file_name=f"{root.name}.zip", mime="application/zip")
