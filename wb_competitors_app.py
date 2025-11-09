# wb_competitors_app.py
# FAST версия: асинхронная скачка фото (aiohttp) + детальный режим (по одному слайду) + Excel с картинками + коллаж + ZIP + автоочистка

import asyncio
import io
import json
import math
import pathlib
import re
import shutil
import zipfile
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse, parse_qs

import pandas as pd
import requests
import streamlit as st
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---- aiohttp (для быстрой скачки) ----
from aiohttp import ClientSession, TCPConnector, ClientTimeout

# ================== НАСТРОЙКИ ==================
# Параллельность
GLOBAL_CONN_LIMIT = 64          # общий лимит одновременных соединений к CDN WB
PER_PRODUCT_WORKERS = 8         # одновременные скачивания слайдов одного товара
# Таймауты
HTTP_TIMEOUT = ClientTimeout(total=20)     # aiohttp
REQ_TIMEOUT = (5, 12)                      # requests (connect, read)
RETRY_TOTAL = 2
# Прочее
DEFAULT_SLIDES = 10
THUMB = (360, 360)             # превью в коллаже
CELL_PX = (160, 160)           # размер картинки в Excel (ширина, высота)

# ================== UI ==================
st.set_page_config(page_title="WB Competitors Saver (FAST + Progress)", page_icon="⚡", layout="wide")
st.title("⚡ WB анализ листинга")
st.caption(
    "Вставь ссылки WB (по одной в строке) → нажми **«Сгенерировать пакет»**.\n"
    "Сервис скачает фото по артикулам (очень быстро в асинхронном режиме), соберёт **Excel с картинками**, **коллаж** и **ZIP**."
)

# ================== УТИЛИТЫ ==================
def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def sanitize_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "WB_Save"
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.U)
    s = re.sub(r"\s+", "_", s)
    return s or "WB_Save"

def new_unique_root(name_hint: str | None = None) -> pathlib.Path:
    base = sanitize_name(name_hint or "WB_Save")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = pathlib.Path.cwd() / f"{base}_{ts}"
    root.mkdir(parents=True, exist_ok=True)
    return root

def parse_input_urls(text: str) -> list[str]:
    return [u.strip() for u in (text or "").splitlines() if u.strip()]

# ---------- requests-сессия (для card JSON, стабильно) ----------
def make_requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=RETRY_TOTAL, connect=RETRY_TOTAL, read=RETRY_TOTAL,
        backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

# ================== WB HELPERS ==================
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

def fetch_card_json(session: requests.Session, nm: str) -> dict | None:
    api = (f"https://card.wb.ru/cards/v2/detail"
           f"?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}")
    r = session.get(api, timeout=REQ_TIMEOUT)
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
    exts = (".webp", ".jpg")  # webp обычно легче
    baskets = [f"https://basket-{i:02d}.wb.ru" for i in range(1, 33)]
    baskets += [f"https://basket-{i:02d}.wbbasket.ru" for i in range(1, 33)]
    urls = []
    for host in baskets:
        base = f"{host}/vol{vol}/part{part}/{nm_id}/images/big/{idx}"
        for ext in exts:
            urls.append(base + ext)
    return urls

# ================== СКАЧКА (ASYNC FAST) ==================
async def download_one_image_async(session: ClientSession, urls: list[str], dest_path: pathlib.Path) -> bool:
    """Пробуем список зеркал по очереди, сохраняем первый успешный."""
    if dest_path.with_suffix(".webp").exists() or dest_path.with_suffix(".jpg").exists():
        return True
    for u in urls:
        try:
            async with session.get(u) as r:
                if r.status == 200:
                    data = await r.read()
                    if data:
                        ext = ".webp" if u.endswith(".webp") else ".jpg"
                        dest = dest_path.with_suffix(ext)
                        dest.write_bytes(data)
                        return True
        except Exception:
            pass
    return False

async def download_product_fast(session: ClientSession, nm: int, pics: int, subdir: pathlib.Path) -> int:
    """Скачка всех слайдов товара с лимитом одновременных задач."""
    ensure_dir(subdir)
    sem = asyncio.Semaphore(PER_PRODUCT_WORKERS)

    async def _one(i: int):
        async with sem:
            urls = candidate_image_urls(nm, i)
            ok = await download_one_image_async(session, urls, subdir / f"{i}")
            return 1 if ok else 0

    tasks = [_one(i) for i in range(1, pics + 1)]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return sum(results)

async def run_fast_batch(items: list[tuple[int, int, pathlib.Path]]) -> dict[int, int]:
    """
    items: [(nm, pics, subdir), ...]
    return: {nm: saved_count}
    """
    connector = TCPConnector(limit=GLOBAL_CONN_LIMIT, ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    result: dict[int, int] = {}
    async with ClientSession(connector=connector, timeout=HTTP_TIMEOUT, headers=headers) as session:
        # запускаем все товары параллельно
        async def _run_one(nm, pics, subdir):
            saved = await download_product_fast(session, nm, pics, subdir)
            result[nm] = saved

        await asyncio.gather(*(_run_one(nm, pics, subdir) for nm, pics, subdir in items))
    return result

# ================== СКАЧКА (DETAILED, SYNC) ==================
def download_product_images_detailed_sync(reqs: requests.Session, nm: int, pics: int,
                                          subdir: pathlib.Path,
                                          progress_bar, status_text) -> int:
    """Последовательно — безопасно обновляем прогресс по каждому слайду."""
    ensure_dir(subdir)
    saved = 0
    progress_bar.progress(0.0)
    for i in range(1, pics + 1):
        urls = candidate_image_urls(nm, i)
        ok = False
        for u in urls:
            try:
                r = reqs.get(u, timeout=REQ_TIMEOUT, stream=False)
                if r.status_code == 200 and int(r.headers.get("Content-Length", "1")) > 0:
                    ext = ".webp" if u.endswith(".webp") else ".jpg"
                    (subdir / f"{i}{ext}").write_bytes(r.content)
                    ok = True
                    break
            except Exception:
                pass
        saved += 1 if ok else 0
        status_text.write(f"Слайд {i}/{pics} — {'OK' if ok else 'пропуск'}")
        progress_bar.progress(i / pics)
    return saved

# ================== ПОСТ-ОБРАБОТКА ==================
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
        df_sum = pd.DataFrame(summary_rows)
        if not df_sum.empty:
            cols = ["Конкурент", "Артикул", "Бренд", "Наименование", "Слайды", "Папка"]
            df_sum = df_sum[[c for c in cols if c in df_sum.columns]]
        df_sum.to_excel(writer, sheet_name="Сводка", index=False)

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

def save_collage(root: pathlib.Path, limit_slides: int = 10) -> pathlib.Path | None:
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

def make_zip_bytes(root: pathlib.Path) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, arcname=str(path.relative_to(root)))
    mem.seek(0)
    return mem.read()

# ================== ИНТЕРФЕЙС ==================
with st.form("form_links"):
    urls_text = st.text_area("Ссылки на товары WB (по одной на строке)", height=160)
    session_name = st.text_input("Имя набора (необязательно)", placeholder="Анализ_товаров")
    detailed = st.checkbox("Детальный прогресс (по фото)", value=False)
    c1, c2 = st.columns(2)
    with c1:
        do_generate = st.form_submit_button("🚀 Сгенерировать пакет")
    with c2:
        do_download_zip = st.form_submit_button("⬇️ Скачать архив")

for key, default in [
    ("zip_bytes", None),
    ("zip_name", None),
    ("excel_bytes", None),
    ("excel_name", None),
    ("collage_bytes", None),
    ("collage_name", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ================== ОСНОВНОЙ ХОД ==================
if do_generate:
    links = parse_input_urls(urls_text)
    if not links:
        st.error("Добавь хотя бы одну ссылку."); st.stop()

    root = new_unique_root(session_name)
    reqs = make_requests_session()

    overall = st.progress(0.0)
    overall_text = st.empty()

    ok_list, err_list = [], []
    total = len(links)

    # Для fast-режима — собираем список товаров и скачиваем пачкой
    fast_items: list[tuple[int, int, pathlib.Path]] = []

    for idx, url in enumerate(links, start=1):
        overall_text.write(f"Товар {idx}/{total}: {url}")

        nm_raw = extract_nm_id(url)
        if not nm_raw:
            err_list.append((url, "Не найден артикул (nm_id)"))
            overall.progress(idx/total); continue

        nm = int(nm_raw)
        try:
            prod = fetch_card_json(reqs, nm_raw)
        except Exception as e:
            err_list.append((url, f"API ошибка: {e}"))
            overall.progress(idx/total); continue

        title, brand, pics = parse_basics(prod)
        if pics <= 0:
            pics = DEFAULT_SLIDES

        subdir = root / f"{idx:03d}_{nm}"
        ensure_dir(subdir)
        (subdir / "meta.json").write_text(
            json.dumps({"url": url, "nm_id": nm, "title": title, "brand": brand,
                        "saved_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        if detailed:
            exp = st.expander(f"📦 {idx}/{total} • nm={nm} • {title or 'Без названия'}", expanded=True)
            with exp:
                pbar = st.progress(0.0)
                line = st.empty()
                saved = download_product_images_detailed_sync(reqs, nm, pics, subdir, pbar, line)
            if saved > 0:
                ok_list.append((url, subdir.name, saved))
            else:
                err_list.append((url, "Не удалось сохранить изображения"))
            overall.progress(idx/total)
        else:
            # в fast-режиме не качаем сразу — сделаем пачкой асинхронно
            fast_items.append((nm, pics, subdir))
            overall.progress(idx/total)

    # если был fast-режим — запускаем асинхронную пачку
    if fast_items:
        with st.spinner("⚡ Быстрая скачка фото (асинхронно)…"):
            try:
                result_map = asyncio.run(run_fast_batch(fast_items))  # {nm: saved}
            except RuntimeError:
                # если Streamlit уже имеет цикл, используем альтернативный запуск
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    result_map = loop.run_until_complete(run_fast_batch(fast_items))
                finally:
                    loop.close()

        # собираем ок/ошибки по fast-списку
        for (nm, pics, subdir) in fast_items:
            saved = int(result_map.get(nm, 0))
            url = None
            meta = subdir / "meta.json"
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    url = m.get("url")
                except Exception:
                    pass
            if saved > 0:
                ok_list.append((url or f"nm={nm}", subdir.name, saved))
            else:
                err_list.append((url or f"nm={nm}", "Не удалось сохранить изображения"))

    # ---- Сводка / Excel / Коллаж ----
    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    summary_rows = []
    for sub in competitors:
        nm = sub.name.split("_")[-1]
        imgs = sorted(list(sub.glob("*.jpg")) + list(sub.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        title = brand = None
        meta = sub / "meta.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                title, brand = m.get("title"), m.get("brand")
            except Exception:
                pass
        summary_rows.append({
            "Конкурент": sub.name.split("_")[0],
            "Артикул": nm,
            "Бренд": brand,
            "Наименование": title,
            "Слайды": len(imgs),
            "Папка": sub.name,
        })

    max_slides = detect_max_slides(root)
    xlsx_path = save_excel_with_images(root, summary_rows, limit_slides=max_slides,
                                       cell_w_px=CELL_PX[0], cell_h_px=CELL_PX[1])
    collage_path = save_collage(root, min(max_slides, 10))

    # Читаем файлы в память
    with open(xlsx_path, "rb") as f:
        excel_bytes = f.read()
    excel_name = xlsx_path.name

    collage_bytes = None
    collage_name = None
    if collage_path and collage_path.exists():
        with open(collage_path, "rb") as f:
            collage_bytes = f.read()
        collage_name = collage_path.name

    # ZIP
    zip_bytes = make_zip_bytes(root)
    zip_name = f"{root.name}.zip"

    # Удаляем папку на сервере
    try:
        shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass

    # В сессию
    st.session_state["zip_bytes"] = zip_bytes
    st.session_state["zip_name"] = zip_name
    st.session_state["excel_bytes"] = excel_bytes
    st.session_state["excel_name"] = excel_name
    st.session_state["collage_bytes"] = collage_bytes
    st.session_state["collage_name"] = collage_name

    st.success("Готово! Пакет сформирован. Временная папка удалена.")
    st.write(f"📊 Excel: {excel_name}")
    if collage_name:
        st.write(f"🖼 Коллаж: {collage_name}")

    if ok_list:
        st.subheader("✅ Сохранены")
        for url, folder, cnt in ok_list:
            st.write(f"- {folder} — {cnt} фото — {url}")
    if err_list:
        st.subheader("⚠️ Ошибки")
        for url, msg in err_list:
            st.write(f"- {url}: {msg}")

    # Кнопки скачивания
    if st.session_state["excel_bytes"]:
        st.download_button("⬇️ Скачать только Excel",
                           data=st.session_state["excel_bytes"],
                           file_name=st.session_state["excel_name"],
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if st.session_state["collage_bytes"]:
        st.download_button("⬇️ Скачать только коллаж (JPG)",
                           data=st.session_state["collage_bytes"],
                           file_name=st.session_state["collage_name"],
                           mime="image/jpeg")
    st.download_button("⬇️ Скачать архив (всё вместе)",
                       data=st.session_state["zip_bytes"],
                       file_name=st.session_state["zip_name"],
                       mime="application/zip")

# Повторная выгрузка ZIP
if do_download_zip:
    if not st.session_state["zip_bytes"]:
        st.error("Архив ещё не готов. Сначала нажми «Сгенерировать пакет».")
    else:
        st.download_button("⬇️ Скачать архив (всё вместе)",
                           data=st.session_state["zip_bytes"],
                           file_name=st.session_state["zip_name"],
                           mime="application/zip")
