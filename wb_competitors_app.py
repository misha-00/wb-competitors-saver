import os
print("Текущий пароль ADMIN_PASSWORD:", os.environ.get("ADMIN_PASSWORD"))


# wb_competitors_app.py
# Доступ по одноразовым ключам + админ-панель
# Ускоренная загрузка, детальный прогресс, уникальные папки, автоочистка

import os
import re
import io
import json
import math
import zipfile
import shutil
import pathlib
import secrets
from datetime import datetime, timedelta
import concurrent.futures as cf

import requests
import streamlit as st
import pandas as pd
from PIL import Image
from io import BytesIO
from urllib.parse import urlparse, parse_qs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# ------- НАСТРОЙКИ -------
# =========================

# Пароль владельца для входа в админ-панель (выдача ключей)
ADMIN_PASSWORD = (
    st.secrets.get("ADMIN_PASSWORD")
    if hasattr(st, "secrets") else None
) or os.environ.get("ADMIN_PASSWORD", "Misha0410!")  # ← вот здесь пароль по умолчанию

AUTH_STORE_PATH = pathlib.Path("auth_store.json")   # локальное хранилище ключей
TOKEN_TTL_HOURS_DEFAULT = 24                        # срок действия ключа, часов

# Производительность
MAX_WORKERS = 24
PER_PRODUCT_WORKERS = 8
REQ_TIMEOUT = (5, 12)
RETRY_TOTAL = 2
DEFAULT_SLIDES = 10
THUMB = (360, 360)
CELL_PX = (160, 160)

# =========================
# ------ АВТОРИЗАЦИЯ ------
# =========================

def _load_store() -> dict:
    if AUTH_STORE_PATH.exists():
        try:
            return json.loads(AUTH_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tokens": {}}  # token -> {issued_at, expires_at, used, note}

def _save_store(store: dict):
    AUTH_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

def _gen_token(n_chars: int = 16) -> str:
    # URL-safe, коротко и удобно
    return secrets.token_urlsafe(n_chars)[:n_chars]

def issue_tokens(n: int, ttl_hours: int, note: str | None = None) -> list[dict]:
    store = _load_store()
    out = []
    now = datetime.utcnow()
    exp = now + timedelta(hours=ttl_hours)
    for _ in range(n):
        token = _gen_token(16)
        while token in store["tokens"]:
            token = _gen_token(16)
        store["tokens"][token] = {
            "issued_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "used": False,
            "note": note or ""
        }
        out.append({"token": token, "expires_at": exp.isoformat(), "note": note or ""})
    _save_store(store)
    return out

def validate_and_consume_token(token: str) -> tuple[bool, str]:
    """Возвращает (ok, message). При успехе помечает ключ как used."""
    token = (token or "").strip()
    if not token:
        return False, "Ключ пустой."
    store = _load_store()
    meta = store["tokens"].get(token)
    if not meta:
        return False, "Ключ не найден."
    if meta.get("used"):
        return False, "Ключ уже использован."
    try:
        exp = datetime.fromisoformat(meta["expires_at"])
    except Exception:
        exp = datetime.utcnow() - timedelta(seconds=1)
    if datetime.utcnow() > exp:
        return False, "Срок действия ключа истёк."
    # валидный — поглощаем
    meta["used"] = True
    store["tokens"][token] = meta
    _save_store(store)
    return True, "Доступ разрешён."

def admin_view():
    st.subheader("🔐 Админ-панель (выдача ключей)")
    with st.form("issue_form"):
        col1, col2 = st.columns([2,1])
        with col1:
            note = st.text_input("Заметка (например, кому выдан)", placeholder="Клиент Иванов / тест")
        with col2:
            ttl = st.number_input("Срок действия, часов", min_value=1, max_value=168, value=TOKEN_TTL_HOURS_DEFAULT, step=1)
        count = st.number_input("Сколько ключей выдать", min_value=1, max_value=100, value=5, step=1)
        btn = st.form_submit_button("Сгенерировать ключи")
    if btn:
        tokens = issue_tokens(int(count), int(ttl), note.strip() if note else None)
        st.success("Ключи сгенерированы. Отправь их пользователям.")
        for t in tokens:
            st.code(t["token"])
        st.caption("Подсказка: каждый ключ работает только один раз.")

    # Просмотр активных/использованных (необязательно)
    with st.expander("Список ключей"):
        store = _load_store()
        rows = []
        for k, v in store["tokens"].items():
            rows.append({
                "token": k,
                "used": v.get("used"),
                "expires_at": v.get("expires_at"),
                "note": v.get("note", "")
            })
        if rows:
            df = pd.DataFrame(rows).sort_values(by=["used","expires_at"])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("Ключей пока нет.")

def auth_gate() -> bool:
    """Возвращает True если пользователь авторизован; иначе рисует форму входа."""
    # Сессия
    if st.session_state.get("authed"):
        return True

    st.title("🔑 Доступ по ключу")
    st.write("Введите одноразовый ключ, выданный владельцем приложения.")
    with st.form("auth_form"):
        token = st.text_input("Ключ доступа", type="password")
        col1, col2 = st.columns(2)
        with col1:
            ok = st.form_submit_button("Войти")
        with col2:
            show_admin = st.form_submit_button("Я владелец")
    if ok:
        success, msg = validate_and_consume_token(token)
        if success:
            st.session_state["authed"] = True
            st.experimental_rerun()
        else:
            st.error(msg)
            return False

    if show_admin:
        # Простейшая проверка пароля владельца
        pwd = st.text_input("Пароль владельца", type="password")
        if st.button("Открыть админ-панель"):
            if pwd == ADMIN_PASSWORD:
                st.session_state["is_admin"] = True
                st.experimental_rerun()
            else:
                st.error("Неверный пароль.")
    # Если нажали «Я владелец» и прошли
    if st.session_state.get("is_admin"):
        admin_view()
    return False

# =========================
# ------ УТИЛИТЫ APP ------
# =========================

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

def make_http_session() -> requests.Session:
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

# ---------- WB helpers ----------
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
    exts = (".webp", ".jpg")
    baskets = [f"https://basket-{i:02d}.wb.ru" for i in range(1, 33)]
    baskets += [f"https://basket-{i:02d}.wbbasket.ru" for i in range(1, 33)]
    urls = []
    for host in baskets:
        base = f"{host}/vol{vol}/part{part}/{nm_id}/images/big/{idx}"
        for ext in exts:
            urls.append(base + ext)
    return urls

# ---------- Загрузки ----------
def download_one_image(session: requests.Session, urls: list[str], dest_path: pathlib.Path) -> bool:
    if dest_path.with_suffix(".webp").exists() or dest_path.with_suffix(".jpg").exists():
        return True
    for u in urls:
        try:
            r = session.get(u, timeout=REQ_TIMEOUT, stream=False)
            if r.status_code == 200 and int(r.headers.get("Content-Length", "1")) > 0:
                ext = ".webp" if u.endswith(".webp") else ".jpg"
                with open(dest_path.with_suffix(ext), "wb") as f:
                    f.write(r.content)
                return True
        except Exception:
            pass
    return False

def download_product_images_fast(session: requests.Session, nm: int, pics: int, subdir: pathlib.Path) -> int:
    ensure_dir(subdir)
    saved = 0
    tasks = list(range(1, pics + 1))
    workers = min(PER_PRODUCT_WORKERS, max(1, math.ceil(pics / 2)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for i in tasks:
            urls = candidate_image_urls(nm, i)
            dest_stub = subdir / f"{i}"
            futures.append(pool.submit(download_one_image, session, urls, dest_stub))
        for fut in cf.as_completed(futures):
            try:
                if fut.result():
                    saved += 1
            except Exception:
                pass
    return saved

def download_product_images_detailed(session: requests.Session, nm: int, pics: int,
                                     subdir: pathlib.Path,
                                     progress_bar, status_text) -> int:
    ensure_dir(subdir)
    saved = 0
    progress_bar.progress(0.0)
    for i in range(1, pics + 1):
        urls = candidate_image_urls(nm, i)
        ok = download_one_image(session, urls, subdir / f"{i}")
        saved += 1 if ok else 0
        status_text.write(f"Слайд {i}/{pics} — {'OK' if ok else 'пропуск'}")
        progress_bar.progress(i / pics)
    return saved

# ---------- Подсчёт слайдов ----------
def detect_max_slides(root: pathlib.Path) -> int:
    max_slides = 0
    for sub in root.iterdir():
        if not sub.is_dir(): continue
        imgs = list(sub.glob("*.jpg")) + list(sub.glob("*.webp"))
        if not imgs: continue
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

# ---------- Excel + изображения ----------
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
        # --- Сводка на русском ---
        df_sum = pd.DataFrame(summary_rows)
        if not df_sum.empty:
            cols = ["Конкурент", "Артикул", "Бренд", "Наименование", "Слайды", "Папка"]
            df_sum = df_sum[[c for c in cols if c in df_sum.columns]]
        df_sum.to_excel(writer, sheet_name="Сводка", index=False)

        # --- Матрица изображений ---
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

# ---------- Коллаж ----------
def save_collage(root: pathlib.Path, limit_slides: int = 10) -> pathlib.Path | None:
    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    if not competitors: return None
    grid, max_rows = [], 0
    for c in competitors:
        imgs = sorted(list(c.glob("*.jpg")) + list(c.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        imgs = imgs[:limit_slides]
        max_rows = max(max_rows, len(imgs))
        grid.append(imgs)
    if max_rows == 0: return None
    cols, rows = len(grid), max_rows
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

# ---------- ZIP ----------
def make_zip_bytes(root: pathlib.Path) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, arcname=str(path.relative_to(root)))
    mem.seek(0)
    return mem.read()

# =========================
# --------- UI -----------
# =========================

st.set_page_config(page_title="WB Competitors Saver (FAST + Progress)", page_icon="⚡", layout="wide")
st.title("⚡ WB анализ листинга")
st.caption(
    "Вставь ссылки WB (по одной в строке) → нажми **«Сгенерировать пакет»**.\n"
    "Сервис параллельно скачает фото по артикулам, соберёт **Excel с картинками**, **коллаж** и **ZIP**."
)

# --- Авторизационный шлюз ---
if not auth_gate():
    st.stop()

with st.form("form_links"):
    urls_text = st.text_area("Ссылки на товары WB (по одной в строке)", height=160)
    session_name = st.text_input("Имя набора (необязательно)", placeholder="Анализ_товаров")
    detailed = st.checkbox("Детальный прогресс (по фото)", value=True)
    c1, c2 = st.columns(2)
    with c1:
        do_generate = st.form_submit_button("🚀 Сгенерировать пакет")
    with c2:
        do_download_zip = st.form_submit_button("⬇️ Скачать архив")

# состояние (в памяти)
for key, default in [
    ("zip_bytes", None), ("zip_name", None),
    ("excel_bytes", None), ("excel_name", None),
    ("collage_bytes", None), ("collage_name", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ---------- Генерация ----------
if do_generate:
    links = parse_input_urls(urls_text)
    if not links:
        st.error("Добавь хотя бы одну ссылку."); st.stop()

    root = new_unique_root(session_name)
    session = make_http_session()

    overall = st.progress(0.0)
    overall_text = st.empty()

    ok_list, err_list = [], []
    total = len(links)

    for idx, url in enumerate(links, start=1):
        overall_text.write(f"Товар {idx}/{total}: {url}")

        nm_raw = extract_nm_id(url)
        if not nm_raw:
            err_list.append((url, "Не найден артикул (nm_id)"))
            overall.progress(idx/total); continue

        nm = int(nm_raw)
        try:
            prod = fetch_card_json(session, nm_raw)
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

        exp = st.expander(f"📦 {idx}/{total} • nm={nm} • {title or 'Без названия'}", expanded=True if detailed else False)
        with exp:
            pbar = st.progress(0.0)
            line = st.empty()
            if detailed:
                saved = download_product_images_detailed(session, nm, pics, subdir, pbar, line)
            else:
                line.write("Скачиваю изображения (ускоренный режим)…")
                saved = download_product_images_fast(session, nm, pics, subdir)
                pbar.progress(1.0)
                line.write(f"Готово: сохранено {saved} из ~{pics}")

        if saved > 0:
            ok_list.append((url, subdir.name, saved))
        else:
            err_list.append((url, "Не удалось сохранить изображения"))

        overall.progress(idx/total)

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

    with open(xlsx_path, "rb") as f:
        excel_bytes = f.read()
    excel_name = xlsx_path.name

    collage_bytes = None
    collage_name = None
    if collage_path and collage_path.exists():
        with open(collage_path, "rb") as f:
            collage_bytes = f.read()
        collage_name = collage_path.name

    zip_bytes = make_zip_bytes(root)
    zip_name = f"{root.name}.zip"

    try:
        shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass

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

    if ok_list:
        st.subheader("✅ Сохранены")
        for url, folder, cnt in ok_list:
            st.write(f"- {folder} — {cnt} фото — {url}")
    if err_list:
        st.subheader("⚠️ Ошибки")
        for url, msg in err_list:
            st.write(f"- {url}: {msg}")

# Повторная выгрузка ZIP
if do_download_zip:
    if not st.session_state["zip_bytes"]:
        st.error("Архив ещё не готов. Сначала нажми «Сгенерировать пакет».")
    else:
        st.download_button("⬇️ Скачать архив (всё вместе)",
                           data=st.session_state["zip_bytes"],
                           file_name=st.session_state["zip_name"],
                           mime="application/zip")
