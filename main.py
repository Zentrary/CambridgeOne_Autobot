import asyncio
import json
import re
import os
import sys
import codecs
import datetime
import time
import traceback
import requests
import xml.etree.ElementTree as ET
import getpass
import hashlib
import html as html_module
from pathlib import Path
from colorama import Fore, Style, init
from playwright.async_api import async_playwright


_WRONG_MARK_RE = re.compile(
    r"\bwrong\b|\bincorrect\b|\bchecked-incorrect\b", re.IGNORECASE
)
_CORRECT_MARK_RE = re.compile(r"\bcorrect\b|\bchecked-correct\b", re.IGNORECASE)

try:
    import pyttsx3

    HAS_TTS = True
except ImportError:
    HAS_TTS = False

init(autoreset=True)


def clear_screen():
    pass


class TeeLogger:
    def __init__(self, log_dir="logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"session_{ts}.log"
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        try:
            self.file = open(self.log_file, "w", encoding="utf-8")
        except Exception:
            self.file = None
            self.original_stdout.write(f"[!] ไม่สามารถเปิด log file: {self.log_file}\n")

    def write(self, message):
        try:
            self.original_stdout.write(message)
        except Exception:
            pass
        if self.file:
            try:
                clean = re.sub(r"\x1b\[[0-9;]*m", "", message)
                self.file.write(clean)
                self.file.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self.original_stdout.flush()
        except Exception:
            pass
        if self.file:
            try:
                self.file.flush()
            except Exception:
                pass

    def close(self):
        if self.file:
            try:
                self.file.close()
            except Exception:
                pass


class CambridgeOneScraper:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.accounts_file = Path("cambridge_accounts.json")
        self.profiles_dir = Path("chrome_data")
        self.answers_db_file = Path("answers_db.json")
        self.active_account = None
        self.playwright = None
        self.data_js_urls = []
        self.data_js_contents = {}
        self.answers_cache = {}
        self.answers_db = self._load_answers_db()
        self.answer_source = None
        self.last_data_js_url = None
        self.log_file_path = None
        self.tts_engine = None
        self.start_time = 0
        self.exercise_times = {}

        if HAS_TTS:
            try:
                self.tts_engine = pyttsx3.init()
                self.tts_engine.setProperty("rate", 140)
            except Exception:
                self.tts_engine = None

    def reset_data_js_tracking(self):
        self.data_js_urls.clear()
        self.data_js_contents.clear()
        print(f"  {Fore.CYAN}[DATA.JS] เคลียร์ข้อมูลเก่าเรียบร้อย{Style.RESET_ALL}")

    def _load_answers_db(self):
        try:
            if self.answers_db_file.exists():
                with open(self.answers_db_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_answers_db(self):
        try:
            with open(self.answers_db_file, "w", encoding="utf-8") as f:
                json.dump(self.answers_db, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def _db_key(self, course, unit, exercise):
        return f"{course}||{unit}||{exercise}"

    def db_get(self, course, unit, exercise):
        return self.answers_db.get(self._db_key(course, unit, exercise))

    def db_set(self, course, unit, exercise, answers):
        self.answers_db[self._db_key(course, unit, exercise)] = answers
        self._save_answers_db()

    def _choose_answer_source(self):

        if self.answer_source:
            return self.answer_source

        print(f"\n  {Fore.CYAN}เลือกระบบหาคำตอบก่อนทำแบบฝึกหัด{Style.RESET_ALL}")
        print(
            f"  {Fore.CYAN}[1] ใช้ฐานข้อมูลคำตอบที่มีอยู่แล้ว (answers_db.json){Style.RESET_ALL}"
        )
        print(f"  {Fore.CYAN}[2] ดึงไฟล์ data.js มาแปลงเป็นคำตอบ{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[3] ถามทุกครั้งก่อนทำแต่ละข้อ{Style.RESET_ALL}")

        try:
            choice = input(
                f"\n  {Fore.CYAN}[>] เลือกระบบ (1-3): {Style.RESET_ALL}"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            choice = "2"

        if choice == "1":
            self.answer_source = "db"
            print(
                f"  {Fore.GREEN}[+] เลือกระบบ: ใช้ฐานข้อมูลคำตอบที่มีอยู่แล้ว{Style.RESET_ALL}"
            )
        elif choice == "3":
            self.answer_source = "ask"
            print(
                f"  {Fore.GREEN}[+] เลือกระบบ: ถามทุกครั้งก่อนทำแต่ละข้อ{Style.RESET_ALL}"
            )
        else:
            self.answer_source = "extract"
            print(
                f"  {Fore.GREEN}[+] เลือกระบบ: ดึงไฟล์ data.js มาแปลงเป็นคำตอบ{Style.RESET_ALL}"
            )

        return self.answer_source

    def _account_key(self, email):
        return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:20]

    def _profile_dir(self, email):
        return self.profiles_dir / self._account_key(email)

    def load_accounts(self):
        try:
            if self.accounts_file.exists():
                with open(self.accounts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def save_account(self, email, password=None):
        email = email.strip()
        if not email:
            return False
        accounts = self.load_accounts()
        key = self._account_key(email)
        existing = next((a for a in accounts if a.get("key") == key), None)
        entry = {"key": key, "email": email}
        if password:
            entry["password"] = password
        elif existing and existing.get("password"):
            entry["password"] = existing["password"]
        accounts = [a for a in accounts if a.get("key") != key]
        accounts.insert(0, entry)
        try:
            with open(self.accounts_file, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def get_saved_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get("key") == key and a.get("password"):
                return a.get("password")
        return None

    def delete_account_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get("key") == key:
                a.pop("password", None)
        try:
            with open(self.accounts_file, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    async def debug_wait(self, seconds=0.5):
        await asyncio.sleep(seconds)

    async def safe_wait_for_load(self, timeout=8000):
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    async def wait_for_loading_spinner(self, timeout=15):
        try:
            await self.page.wait_for_selector(
                '.loading, .spinner, [class*="spinner"], [class*="loading"]',
                state="detached",
                timeout=timeout * 1000,
            )
        except Exception:
            pass

    async def check_video_true_false(self):
        try:
            for frame in self.page.frames:
                has_choice_interaction = await frame.evaluate(
                    """
                    () => {
                        return document.querySelectorAll('.choice_interaction').length > 0;
                    }
                """
                )
                if has_choice_interaction:
                    return True
            return False
        except Exception:
            return False

    async def _get_activity_frame(self, timeout=5, force_refresh=False):

        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            for frame in self.page.frames:
                try:
                    count = await frame.evaluate(
                        """
                        () => document.querySelectorAll('.choice_interaction, .wrapper-dropdown, .input-radio, .input-checkbox, input[type="text"], textarea, .drag_element, .drop_area, .om-textgap-element, [draggable="true"]').length
                    """
                    )
                    if count > 0:
                        return frame
                except Exception:
                    continue
            await asyncio.sleep(0.3)
        return None

    async def _find_skip_button(self):

        for frame in self.page.frames:
            try:
                btns = await frame.query_selector_all('button, a, [role="button"]')
            except Exception:
                continue
            for btn in btns:
                try:
                    if not await btn.is_visible():
                        continue
                    if (await btn.get_attribute("aria-hidden") or "").lower() == "true":
                        continue

                    txt = (await btn.inner_text() or "").strip().lower()
                    label = (
                        (await btn.get_attribute("aria-label") or "").strip().lower()
                    )
                    name = txt or label
                    if name not in ("skip", "skip exercise", "ข้าม", "ข้ามแบบฝึกหัด"):
                        continue

                    box = await btn.bounding_box()
                    cls = (await btn.get_attribute("class") or "").lower()
                    if (
                        box
                        and box["width"] >= 8
                        and box["height"] >= 8
                        and "disabled" not in cls
                    ):
                        return btn
                except Exception:
                    continue
        return None

    async def _find_button_by_names(self, names, max_len=32):

        wanted = tuple(n.lower() for n in names)
        for frame in self.page.frames:
            try:
                btns = await frame.query_selector_all('button, a, [role="button"]')
            except Exception:
                continue
            for btn in btns:
                try:
                    if not await btn.is_visible():
                        continue
                    txt = (await btn.inner_text() or "").strip().lower()
                    label = (
                        (await btn.get_attribute("aria-label") or "").strip().lower()
                    )
                    title = (await btn.get_attribute("title") or "").strip().lower()
                    candidates = [c for c in (txt, label, title) if c]
                    if not candidates:
                        continue
                    if not any(c in wanted or c.startswith(wanted) for c in candidates):
                        continue
                    if len(txt) > max_len:
                        continue
                    box = await btn.bounding_box()
                    if box and box["width"] >= 8 and box["height"] >= 8:
                        return btn
                except Exception:
                    continue
        return None

    async def _find_recording_button(self):

        for frame in self.page.frames:
            try:
                btns = await frame.query_selector_all(
                    '#recording-button, button[aria-label="Recording Button"], button[aria-label="recording button"], button.speak-btn, .speak-button, [data-action="speak"]'
                )
            except Exception:
                continue
            for btn in btns:
                try:
                    if not await btn.is_visible():
                        continue
                    box = await btn.bounding_box()
                    if box and box["width"] >= 20 and box["height"] >= 20:
                        return btn
                except Exception:
                    continue
        return None

    async def _wait_for_stop_button(self, timeout=10):

        start = time.time()
        while time.time() - start < timeout:
            btn = await self._find_button_by_names(
                (
                    "stop recording",
                    "stop",
                    "หยุด",
                    "หยุดอัดเสียง",
                    "stop recording button",
                )
            )
            if btn:
                return btn
            await asyncio.sleep(0.4)
        return None

    async def _has_answer_inputs(self, frame):

        try:
            return bool(
                await frame.evaluate(
                    """
                () => {
                    const wraps = [...document.querySelectorAll('.content-wrap')]
                        .filter(w => !w.classList.contains('activity--hidden'));
                    const root = wraps.length ? wraps[wraps.length - 1] : document.body;
                    if (!root) return false;
                    return !!root.querySelector(
                        'input:not([type="hidden"]):not([type="file"]):not([type="button"]), ' +
                        'textarea, .wrapper-dropdown, .listbox, [draggable="true"], ' +
                        '.om-textgap-element, .drop_area'
                    );
                }
            """
                )
            )
        except Exception:
            return False

    async def _detect_speaking_ui(self):

        btn = await self._find_recording_button()
        if not btn:
            return False
        for frame in self.page.frames:
            try:
                if "speech-dialogue" in (frame.url or ""):
                    continue
            except Exception:
                pass
            if await self._has_answer_inputs(frame):
                print(
                    f"  {Fore.CYAN}[*] พบปุ่ม Speak แต่มีช่องตอบปกติ — ถือเป็นแบบฝึกหัดทั่วไป{Style.RESET_ALL}"
                )
                return False
        return True

    async def _get_speech_text(self):

        chunks = []
        for frame in self.page.frames:
            try:
                if "speech-dialogue" not in (frame.url or ""):
                    continue
                chunks.append(await frame.inner_text("body"))
            except Exception:
                continue
        return "\n".join(chunks)

    async def _wait_for_speaking_outcome(self, timeout=45):

        start = time.time()
        while time.time() - start < timeout:
            if await self._is_result_screen():
                return ("done", None)

            skip_btn = await self._find_skip_button()
            if skip_btn:
                return ("skip", skip_btn)

            if time.time() - start > 5:
                btn = await self._find_recording_button()
                if btn:
                    try:
                        label = (await btn.inner_text() or "").strip().lower()
                    except Exception:
                        label = ""
                    if label == "speak":
                        return ("advanced", None)

            await asyncio.sleep(0.75)

        return ("timeout", None)

    async def _click_activity_next(self, timeout=8):

        selectors = [
            'a[title="Next"]',
            'button[title="Next"]',
            'a.green-btn[title="Next"]',
            ".btn-next",
            'button:has-text("Next")',
            'a:has-text("Next")',
        ]
        for _ in range(int(timeout * 2)):
            frames_to_search = [self.page] + list(self.page.frames)
            for frame in frames_to_search:
                for sel in selectors:
                    try:
                        btn = await frame.query_selector(sel)
                        if not btn or not await btn.is_visible():
                            continue
                        txt = (await btn.inner_text() or "").strip().lower()
                        title = (await btn.get_attribute("title") or "").strip().lower()
                        if "next activity" in txt or "next activity" in title:
                            continue
                        cls = (await btn.get_attribute("class") or "").lower()
                        if "disabled" in cls or "inactive" in cls:
                            continue
                        await btn.scroll_into_view_if_needed()
                        await btn.click(force=True)
                        await self.debug_wait(2)
                        return True
                    except Exception:
                        continue
            await asyncio.sleep(0.5)
        return False

    async def _find_next_activity_button(self):
        selectors = [
            "a.nextActivityBtn",
            'a:has-text("NEXT ACTIVITY")',
            'button:has-text("NEXT ACTIVITY")',
            'a:has-text("Next activity")',
            'button:has-text("Next activity")',
            ".nextActivityBtn.btn",
        ]
        for frame in self.page.frames:
            for sel in selectors:
                try:
                    btn = await frame.query_selector(sel)
                    if btn and await btn.is_visible():
                        return btn
                except Exception:
                    continue
        return None

    async def auto_fill_and_check(self, exercise_name, answers_by_file):
        self.start_time = time.time()
        print(
            f"\n  {Fore.CYAN}[*] กำลังเติมคำตอบอัตโนมัติ: {exercise_name}{Style.RESET_ALL}"
        )

        result = {
            "success": False,
            "reason": "",
            "filled": 0,
            "total": 0,
            "failed": [],
            "score": None,
        }

        is_speaking = await self.handle_speaking_activity()
        if is_speaking:
            await self.debug_wait(2)
            result["success"] = True
            result["reason"] = "OK (Speaking Activity)"
            return result

        all_questions = []
        if answers_by_file:
            for _, questions in answers_by_file.items():
                all_questions.extend(questions)

        if not all_questions:
            result["reason"] = "ไม่มีเฉลยให้เติม"
            print(f"  {Fore.YELLOW}[!] {result['reason']}{Style.RESET_ALL}")
            return result

        type_counts = {}
        for q in all_questions:
            t = q.get("type", "Unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"  {Fore.CYAN}[*] ประเภทคำถามที่พบ: {type_counts}{Style.RESET_ALL}")

        mc_questions = [q for q in all_questions if q.get("type") == "Multiple Choice"]
        is_multi_page = await self.is_multi_question_mc()

        if is_multi_page and len(all_questions) > 0:
            print(
                f"  {Fore.CYAN}[*] ตรวจพบ Multi-Page ({len(all_questions)} คำถาม) — เริ่มทำทีละหน้าจนจบ{Style.RESET_ALL}"
            )
            success = await self.continue_multi_question(exercise_name, answers_by_file)
            if success:
                result["success"] = True
                result["reason"] = "OK (Multi-Page)"
                result["filled"] = 1
                result["total"] = 1
            else:
                result["reason"] = "Multi-Page ทำไม่สำเร็จ"
            return result

        print(f"  {Fore.CYAN}[*] โหมด Single-Page{Style.RESET_ALL}")

        dd_questions = [
            q
            for q in all_questions
            if q.get("type") in ("Drag & Drop", "Drag Drop", "Drag-and-Drop")
        ]
        if dd_questions:
            print(
                f"  {Fore.CYAN}[*] จะเติม Drag & Drop {len(dd_questions)} ข้อ...{Style.RESET_ALL}"
            )
            for q in dd_questions:
                filled = await self._fill_click_to_fill(q)
                if filled:
                    result["filled"] += 1
                else:
                    result["failed"].append(q.get("question_number", "Drag & Drop"))
            result["total"] += len(dd_questions)
            if result["filled"] < len(dd_questions):
                print(
                    f"  {Fore.YELLOW}[!] เติม Drag & Drop ได้ {result['filled']}/{len(dd_questions)} ช่อง — ลองกด Check ต่อ{Style.RESET_ALL}"
                )
        await self.debug_wait(1)

        te_questions = [q for q in all_questions if q.get("type") == "Text Entry"]
        if te_questions:
            print(
                f"  {Fore.CYAN}[*] จะเติม Text Entry {len(te_questions)} ช่อง...{Style.RESET_ALL}"
            )
            for idx, q in enumerate(te_questions):
                ok = await self._fill_text_entry(q, idx)
                if ok:
                    result["filled"] += 1
                else:
                    result["failed"].append(q.get("response_id", f"TE#{idx + 1}"))
            result["total"] += len(te_questions)
        await self.debug_wait(1)

        mc_questions = [q for q in all_questions if q.get("type") == "Multiple Choice"]
        radio_questions = [q for q in mc_questions if not q.get("is_multiple_answers")]
        checkbox_questions = [q for q in mc_questions if q.get("is_multiple_answers")]

        if radio_questions:
            print(
                f"  {Fore.CYAN}[*] จะเติม Multiple Choice (Radio) {len(radio_questions)} ข้อ...{Style.RESET_ALL}"
            )
            for q in radio_questions:
                q_idx = q.get("question_number", 1) - 1
                ok = await self._fill_multiple_choice_by_index(q, q_idx)
                if ok:
                    result["filled"] += 1
                else:
                    result["failed"].append(
                        q.get("response_id", f'MC#{q.get("question_number")}')
                    )
            result["total"] += len(radio_questions)

        if checkbox_questions:
            print(
                f"  {Fore.CYAN}[*] จะเติม Multiple Choice (Checkbox) {len(checkbox_questions)} ข้อ...{Style.RESET_ALL}"
            )
            for q in checkbox_questions:
                ok = await self._fill_checkbox(q)
                if ok:
                    result["filled"] += 1
                else:
                    result["failed"].append(
                        q.get("response_id", f'CB#{q.get("question_number")}')
                    )
            result["total"] += len(checkbox_questions)

        dd2_questions = [q for q in all_questions if q.get("type") == "Dropdown"]
        print(
            f"  {Fore.CYAN}[*] จะเติม Dropdown {len(dd2_questions)} ช่อง...{Style.RESET_ALL}"
        )
        result["total"] += len(dd2_questions)
        filled_ok = 0
        failed = []
        for idx, q in enumerate(dd2_questions, 1):
            ok = await self._fill_dropdown(q)
            if ok:
                filled_ok += 1
            else:
                failed.append(q.get("response_id", f"#{idx}"))
            await self.debug_wait(0.4)

        result["filled"] += filled_ok
        result["failed"].extend(failed)

        if dd2_questions and filled_ok == 0:
            result["reason"] = f"เติม dropdown ไม่ได้เลย (0/{len(dd2_questions)})"
            print(
                f"  {Fore.RED}[!] {result['reason']} — ยกเลิกการกด Check{Style.RESET_ALL}"
            )
            return result

        await self.debug_wait(2)

        success = await self._click_check_button()
        if success:
            result["success"] = True
            result["reason"] = "OK"
            print(f"  {Fore.GREEN}[+] เติมคำตอบและกด Check สำเร็จ{Style.RESET_ALL}")

            await self.debug_wait(2)

            try:
                body_text = await self.page.inner_text("body")
                score_match = re.search(
                    r"You scored (\d+) out of (\d+)", body_text, re.IGNORECASE
                )
                if score_match:
                    result["score"] = f"{score_match.group(1)}/{score_match.group(2)}"
                    print(
                        f"  {Fore.GREEN}[+] คะแนน: {result['score']}{Style.RESET_ALL}"
                    )
            except Exception:
                pass

        else:
            print(
                f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check — ลองรออีก 3 วินาที...{Style.RESET_ALL}"
            )
            await self.debug_wait(3)
            success = await self._click_check_button()
            if success:
                result["success"] = True
                result["reason"] = "OK (Retry Check)"
                print(
                    f"  {Fore.GREEN}[+] เติมคำตอบและกด Check สำเร็จ (รอบ 2){Style.RESET_ALL}"
                )
                await self.debug_wait(2)
            else:
                result["reason"] = "ไม่พบปุ่ม Check"
                print(f"  {Fore.YELLOW}[!] {result['reason']}{Style.RESET_ALL}")

        elapsed = time.time() - self.start_time
        self.exercise_times[exercise_name] = elapsed
        print(f"  {Fore.CYAN}[*] ใช้เวลาไป: {elapsed:.2f} วินาที{Style.RESET_ALL}")

        return result

    async def is_multi_question_mc(self):

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False
            result = await frame.evaluate(
                """
                () => {
                    const steps = document.querySelectorAll('.progress-bar .step');
                    return steps.length >= 2;
                }
            """
            )
            return bool(result)
        except Exception:
            return False

    async def get_current_question_number(self):
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return 0

            num = await frame.evaluate(
                """
                () => {
                    const currentStep = document.querySelector('.progress-bar .step.current');
                    if (currentStep) {
                        const numEl = currentStep.querySelector('.step-number');
                        if (numEl) {
                            const n = parseInt(numEl.innerText.trim());
                            if (!isNaN(n)) return n;
                        }
                        const steps = document.querySelectorAll('.progress-bar .step');
                        for (let i = 0; i < steps.length; i++) {
                            if (steps[i].classList.contains('current')) {
                                return i + 1;
                            }
                        }
                    }
                    
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    for (const wrap of visibleWraps) {
                        const id = wrap.id || '';
                        const m = id.match(/content_wrap_(\\d+)/);
                        if (m) {
                            return parseInt(m[1]) + 1;
                        }
                    }
                    
                    return 0;
                }
            """
            )
            return num if num else 0
        except Exception:
            return 0

    async def get_current_question_text(self):
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return {"options": [], "index": -1, "total": 0, "type": "unknown"}

            result = await frame.evaluate(
                """
                () => {
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) {
                        return {options: [], index: -1, total: 0};
                    }
                    
                    const activeWrap = visibleWraps[0];
                    const id = activeWrap.id || '';
                    const m = id.match(/content_wrap_(\\d+)/);
                    const idx = m ? parseInt(m[1]) : -1;
                    
                    const options = [];
                    let qtype = 'unknown';
                    
                    const hasRadio = activeWrap.querySelectorAll('.input-radio, .is-radiobutton-choice-text').length > 0;
                    const hasCheckbox = activeWrap.querySelectorAll('.input-checkbox, .is-checkbox-choice-text').length > 0;
                    const hasSelect = activeWrap.querySelectorAll('select').length > 0;
                    const hasDropdown = activeWrap.querySelectorAll('.dropdown, .is-dropdown, [role="listbox"]').length > 0;
                    const hasTextInput = activeWrap.querySelectorAll('input[type="text"], input:not([type]), textarea').length > 0;
                    const hasDragDrop = activeWrap.querySelectorAll('[draggable="true"], .draggable, .drag-item').length > 0;
                    
                    if (hasRadio || hasCheckbox) qtype = 'Multiple Choice';
                    else if (hasSelect || hasDropdown) qtype = 'Dropdown';
                    else if (hasTextInput) qtype = 'Text Entry';
                    else if (hasDragDrop) qtype = 'Drag Drop';
                    
                    if (hasRadio) {
                        activeWrap.querySelectorAll('.is-radiobutton-choice-text').forEach(t => {
                            options.push(t.innerText.trim());
                        });
                    }
                    
                    if (hasCheckbox && options.length === 0) {
                        activeWrap.querySelectorAll('.is-checkbox-choice-text').forEach(t => {
                            options.push(t.innerText.trim());
                        });
                    }
                    
                    if ((hasSelect || hasDropdown) && options.length === 0) {
                        const selects = activeWrap.querySelectorAll('select');
                        selects.forEach(sel => {
                            for (const opt of sel.options) {
                                options.push(opt.innerText.trim());
                            }
                        });
                        const dropdowns = activeWrap.querySelectorAll('.dropdown, .is-dropdown, [role="listbox"]');
                        dropdowns.forEach(dd => {
                            dd.querySelectorAll('li, [role="option"], .dropdown-option').forEach(opt => {
                                options.push(opt.innerText.trim());
                            });
                        });
                    }
                    
                    const total = document.querySelectorAll('.content-wrap').length;
                    
                    return {options: options, index: idx, total: total, type: qtype};
                }
            """
            )
            return (
                result
                if result
                else {"options": [], "index": -1, "total": 0, "type": "unknown"}
            )
        except Exception:
            return {"options": [], "index": -1, "total": 0, "type": "unknown"}

    async def _get_active_wrap(self, frame):

        try:
            wraps = await frame.query_selector_all(
                ".content-wrap:not(.activity--hidden)"
            )
            for w in wraps:
                try:
                    if await w.is_visible():
                        return w
                except Exception:
                    continue
            return wraps[0] if wraps else None
        except Exception:
            return None

    @staticmethod
    def _id_key(value):
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    async def _option_text(self, option):
        txt = ""
        try:
            el = await option.query_selector(
                ".is-radiobutton-choice-text, .is-checkbox-choice-text"
            )
            if el:
                txt = (await el.inner_text()) or ""
        except Exception:
            pass
        if not txt.strip():
            try:
                img = await option.query_selector("img")
                if img:
                    txt = (await img.get_attribute("alt")) or ""
            except Exception:
                pass
        return self._normalize_text(txt)

    async def _option_matches_id(self, option, correct_id):
        key = self._id_key(correct_id)
        if not key:
            return False
        try:
            oid = self._id_key(await option.get_attribute("id") or "")
            cls = self._id_key(await option.get_attribute("class") or "")
            return key in oid or key in cls
        except Exception:
            return False

    async def _click_option(self, frame, option):
        try:
            await option.scroll_into_view_if_needed()
            await option.click()
            return True
        except Exception:
            try:
                await frame.evaluate("(el) => el.click()", option)
                return True
            except Exception:
                return False

    async def _fill_multiple_choice_for_current(self, q, choice_index):
        correct_text = self._normalize_text(q.get("correct_answer", ""))
        correct_answers = [
            self._normalize_text(a) for a in (q.get("correct_answers") or []) if a
        ]
        correct_id = q.get("correct_id", "")
        is_multiple = q.get("is_multiple_answers", False)

        if not correct_text and not correct_answers and not correct_id:
            print(f"  {Fore.YELLOW}[!] ไม่มีข้อมูลคำตอบให้เลือก{Style.RESET_ALL}")
            return False

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False
            wrap = await self._get_active_wrap(frame)
            if not wrap:
                return False

            choices = await wrap.query_selector_all(".choice_interaction")
            if not choices:
                choices = await wrap.query_selector_all(
                    '.choice, [role="group"], .question-block'
                )
            if not choices:
                return False

            idx = choice_index if 0 <= choice_index < len(choices) else 0
            choice = choices[idx]

            options = await choice.query_selector_all(".input-radio, .input-checkbox")
            if not options:
                options = await choice.query_selector_all(
                    'input[type="radio"], input[type="checkbox"]'
                )
            if not options:

                options = await wrap.query_selector_all(
                    '.input-radio, .input-checkbox, input[type="radio"], input[type="checkbox"]'
                )
            if not options:
                return False

            targets = (
                correct_answers if (is_multiple and correct_answers) else [correct_text]
            )
            print(
                f"  {Fore.CYAN}[*] กำลังทำ MC ข้อ {idx + 1}/{len(choices)}: {targets}{Style.RESET_ALL}"
            )

            clicked = 0
            for target in targets:
                target_norm = self._normalize_text(target)
                matched_option = None

                if len(targets) == 1 and correct_id:
                    for opt in options:
                        if await self._option_matches_id(opt, correct_id):
                            matched_option = opt
                            break

                if matched_option is None and target_norm:
                    for opt in options:
                        otxt = await self._option_text(opt)
                        if otxt and otxt == target_norm:
                            matched_option = opt
                            break
                    if matched_option is None:
                        for opt in options:
                            otxt = await self._option_text(opt)
                            if (
                                otxt
                                and target_norm
                                and (otxt in target_norm or target_norm in otxt)
                            ):
                                matched_option = opt
                                break

                if matched_option is None and len(targets) == 1 and correct_id:
                    choice_ids = q.get("choice_ids") or []
                    if correct_id in choice_ids:
                        pos = choice_ids.index(correct_id)
                        if 0 <= pos < len(options):
                            matched_option = options[pos]

                if matched_option is None:
                    continue

                try:
                    aria = (
                        await matched_option.get_attribute("aria-checked") or ""
                    ).lower()
                    if aria == "true":
                        clicked += 1
                        continue
                except Exception:
                    pass

                if await self._click_option(frame, matched_option):
                    clicked += 1
                    await self.debug_wait(0.35)

            if clicked:
                print(
                    f"      {Fore.GREEN}[OK] เลือกคำตอบสำเร็จ ({clicked}){Style.RESET_ALL}"
                )
                return True
            print(f"      {Fore.YELLOW}[!] เลือกคำตอบไม่สำเร็จ{Style.RESET_ALL}")
            return False
        except Exception as e:
            print(
                f"  {Fore.RED}[!] _fill_multiple_choice_for_current error: {e}{Style.RESET_ALL}"
            )
            return False

    async def _fill_dropdown_for_current(self, q, choice_index):

        correct_text = self._normalize_text(q.get("correct_answer", ""))
        if not correct_text:
            print(f"  {Fore.YELLOW}[!] ไม่มีคำตอบสำหรับ Dropdown{Style.RESET_ALL}")
            return False

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False
            wrap = await self._get_active_wrap(frame)
            if not wrap:
                return False

            wrappers = await wrap.query_selector_all(".wrapper-dropdown")
            wrappers = [w for w in wrappers if await w.is_visible()]
            if not wrappers:
                return False

            idx = choice_index if 0 <= choice_index < len(wrappers) else 0
            print(
                f"  {Fore.CYAN}[*] กำลังทำ Dropdown {idx + 1}/{len(wrappers)}: '{correct_text}'{Style.RESET_ALL}"
            )
            ok = await self._select_dropdown_value(frame, wrappers[idx], correct_text)
            if ok:
                print(f"      {Fore.GREEN}[OK] เลือก Dropdown สำเร็จ{Style.RESET_ALL}")
            return ok
        except Exception as e:
            print(
                f"  {Fore.RED}[!] _fill_dropdown_for_current error: {e}{Style.RESET_ALL}"
            )
            return False

    async def _select_dropdown_value(self, frame, wrapper, correct_text):

        if not correct_text:
            return False
        target_norm = self._normalize_text(correct_text)

        toggle = await wrapper.query_selector(
            'button.drop-label, button.listbox__label, button[role="combobox"], button'
        )
        if toggle is None:
            print(f"      {Fore.YELLOW}[!] ไม่พบปุ่มเปิด dropdown{Style.RESET_ALL}")
            return False

        try:
            await toggle.scroll_into_view_if_needed()
        except Exception:
            pass

        for attempt in range(4):
            try:
                await toggle.click(force=True)
            except Exception:
                try:
                    box = await toggle.bounding_box()
                    if box:
                        await self.page.mouse.click(
                            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                        )
                except Exception:
                    pass
            await self.debug_wait(0.6)

            popup_check = await wrapper.query_selector(
                '.popup.listbox__popup, .listbox__popup, .popup, [role="listbox"]'
            )
            if popup_check:
                try:
                    is_visible = await popup_check.is_visible()
                    if is_visible:
                        break
                except Exception:
                    pass

        await self.debug_wait(0.5)

        popup = await wrapper.query_selector(
            '.popup.listbox__popup, .listbox__popup, .popup, [role="listbox"]'
        )
        items = []
        if popup:
            try:
                items = await popup.query_selector_all(
                    'li.listbox__choice, li[role="option"], li, .dropdown-option'
                )
            except Exception:
                items = []
        if not items:
            try:
                items = await wrapper.query_selector_all(
                    'li.listbox__choice, li[role="option"], li, .dropdown-option'
                )
            except Exception:
                items = []

        if not items:
            print(
                f"      {Fore.YELLOW}[!] ไม่พบรายการตัวเลือกใน dropdown{Style.RESET_ALL}"
            )
            return False

        def clean(s):
            return re.sub(r"[^a-zA-Z0-9]", "", (s or "")).lower()

        matched = None
        matched_txt = ""
        for item in items:
            try:
                txt = self._normalize_text((await item.inner_text()))
            except Exception:
                continue
            if not txt:
                continue
            if txt == target_norm:
                matched = item
                matched_txt = txt
                break
        if matched is None:
            for item in items:
                try:
                    txt = self._normalize_text((await item.inner_text()))
                except Exception:
                    continue
                if not txt:
                    continue
                if clean(txt) and clean(txt) == clean(target_norm):
                    matched = item
                    matched_txt = txt
                    break
        if matched is None:
            for item in items:
                try:
                    txt = self._normalize_text((await item.inner_text()))
                except Exception:
                    continue
                if not txt:
                    continue
                if target_norm in txt or txt in target_norm:
                    matched = item
                    matched_txt = txt
                    break

        if matched is None:
            all_txt = []
            for item in items:
                try:
                    all_txt.append(self._normalize_text((await item.inner_text())))
                except Exception:
                    pass
            print(
                f"      {Fore.YELLOW}[!] ไม่พบ '{target_norm}' ในตัวเลือก: {all_txt[:5]}{Style.RESET_ALL}"
            )
            return False

        try:
            await matched.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await matched.click(force=True)
        except Exception:
            try:
                box = await matched.bounding_box()
                if box:
                    await self.page.mouse.click(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                else:
                    await frame.evaluate("(el) => el.click()", matched)
            except Exception:
                return False
        await self.debug_wait(0.6)
        print(
            f"      {Fore.GREEN}[OK] เลือก Dropdown = '{matched_txt}'{Style.RESET_ALL}"
        )
        return True

    async def _fill_text_entry_for_current(self, q, choice_index):

        correct_text = self._normalize_text(q.get("correct_answer", ""))
        if not correct_text:
            answers = q.get("answers") or []
            correct_text = self._normalize_text(answers[0] if answers else "")
        if not correct_text:
            print(f"  {Fore.YELLOW}[!] ไม่มีคำตอบสำหรับ Text Entry{Style.RESET_ALL}")
            return False

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False
            wrap = await self._get_active_wrap(frame)
            if not wrap:
                return False

            inputs = await wrap.query_selector_all(
                'input[type="text"], input:not([type]), textarea'
            )
            inputs = [
                i for i in inputs if await i.is_visible() and await i.is_enabled()
            ]
            if not inputs:
                return False

            idx = choice_index if 0 <= choice_index < len(inputs) else 0
            inp = inputs[idx]
            print(
                f"  {Fore.CYAN}[*] กำลังพิมพ์ Text Entry ช่อง {idx + 1}/{len(inputs)}: '{correct_text}'{Style.RESET_ALL}"
            )

            await inp.scroll_into_view_if_needed()
            await inp.click()
            await inp.fill("")
            await inp.type(correct_text, delay=10)
            await inp.press("Tab")
            await self.debug_wait(0.25)
            return True
        except Exception as e:
            print(
                f"  {Fore.RED}[!] _fill_text_entry_for_current error: {e}{Style.RESET_ALL}"
            )
            return False

    async def _fill_drag_drop_for_current(self, q, choice_index):

        correct_text = self._normalize_text(q.get("correct_answer", ""))
        if not correct_text:
            print(f"  {Fore.YELLOW}[!] ไม่มีคำตอบสำหรับ Drag-and-Drop{Style.RESET_ALL}")
            return False

        print(
            f"  {Fore.CYAN}[*] เติม Drag-and-Drop ที่ content_wrap_{choice_index}: '{correct_text}'{Style.RESET_ALL}"
        )
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False

            dropped = await frame.evaluate(
                """
                (args) => {
                    const [corrText] = args;
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) return false;
                    const activeWrap = visibleWraps[0];

                    const draggables = activeWrap.querySelectorAll('[draggable="true"], .draggable, .drag-item');
                    let matched = false;
                    for (const d of draggables) {
                        const txt = d.innerText.trim();
                        if (txt === corrText || txt.includes(corrText) || corrText.includes(txt)) {
                            const targets = activeWrap.querySelectorAll('.drop-target, [droppable], .dropzone');
                            for (const t of targets) {
                                try {
                                    d.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: new DataTransfer() }));
                                    t.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true }));
                                    t.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }));
                                    matched = true;
                                    break;
                                } catch (e) { continue; }
                            }
                            if (matched) break;
                        }
                    }
                    return matched;
                }
            """,
                [correct_text],
            )

            if dropped:
                await self.debug_wait(0.8)
                print(
                    f"      {Fore.GREEN}[OK] ลากวาง Drag-and-Drop สำเร็จ{Style.RESET_ALL}"
                )
                return True
            return False
        except Exception as e:
            print(
                f"  {Fore.RED}[!] _fill_drag_drop_for_current error: {e}{Style.RESET_ALL}"
            )
            return False

    async def _click_next_button_if_present(self, timeout=10):
        next_selectors = [
            'a[title="Next"]',
            'a.green-btn[title="Next"]',
            "a.nextActivityBtn:not([hidden])",
            'a:has-text("Next")',
            'button:has-text("Next")',
            "a.btn.green-btn",
            'button[data-event="forward"]',
            ".btn-next",
        ]

        frames_to_search = [self.page]
        activity_frame = await self._get_activity_frame(timeout=2)
        if activity_frame:
            frames_to_search.append(activity_frame)
        frames_to_search.extend(self.page.frames)

        for attempt in range(int(timeout * 2)):
            for frame in frames_to_search:
                for selector in next_selectors:
                    try:
                        btn = await frame.query_selector(selector)
                        if not btn:
                            continue
                        is_visible = await btn.is_visible()
                        if not is_visible:
                            continue

                        btn_text = (await btn.inner_text()).strip()
                        btn_title = (await btn.get_attribute("title") or "").strip()

                        is_next = (
                            btn_text.lower() == "next"
                            or btn_title.lower() == "next"
                            or "next" in btn_text.lower()
                        )

                        if not is_next:
                            continue

                        disabled_attr = await btn.get_attribute("disabled")
                        class_attr = (await btn.get_attribute("class") or "").lower()
                        is_disabled = (
                            disabled_attr is not None
                            or "disabled" in class_attr
                            or "inactive" in class_attr
                        )

                        if is_disabled:
                            continue

                        print(
                            f"  {Fore.GREEN}[+] พบปุ่ม Next - กำลังกด...{Style.RESET_ALL}"
                        )
                        await btn.scroll_into_view_if_needed()
                        await btn.click(force=True)
                        await self.debug_wait(2)
                        return True
                    except Exception:
                        continue
            await asyncio.sleep(0.5)

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Next{Style.RESET_ALL}")
        return False

    async def wait_for_next_question(self, timeout=15, expected_next_num=None):
        try:
            old_num = await self.get_current_question_number()
            old_data = await self.get_current_question_text()
            old_options = old_data.get("options", []) if old_data else []
            old_index = old_data.get("index", -1) if old_data else -1

            print(
                f"  {Fore.CYAN}[*] รอหน้าเปลี่ยน (old_num={old_num}, old_idx={old_index}, opts={len(old_options)})...{Style.RESET_ALL}"
            )

            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                new_num = await self.get_current_question_number()
                new_data = await self.get_current_question_text()
                new_options = new_data.get("options", []) if new_data else []
                new_index = new_data.get("index", -1) if new_data else -1

                if await self._is_result_screen():
                    print(f"  {Fore.GREEN}[+] ขึ้นหน้าผลคะแนนแล้ว{Style.RESET_ALL}")
                    return True

                if expected_next_num is not None:
                    if new_num == expected_next_num:
                        print(
                            f"  {Fore.GREEN}[+] ถึงข้อที่คาดหวัง: {new_num}{Style.RESET_ALL}"
                        )
                        return True
                else:
                    if new_num != old_num:
                        print(
                            f"  {Fore.GREEN}[+] progress เปลี่ยน: {old_num} -> {new_num}{Style.RESET_ALL}"
                        )
                        return True
                    if new_index != old_index:
                        print(
                            f"  {Fore.GREEN}[+] index เปลี่ยน: {old_index} -> {new_index}{Style.RESET_ALL}"
                        )
                        return True
                    if new_options and old_options and new_options != old_options:
                        print(f"  {Fore.GREEN}[+] ตัวเลือกเปลี่ยน{Style.RESET_ALL}")
                        return True

                await asyncio.sleep(0.5)
            return False
        except Exception:
            return False

    async def _is_result_screen(self):

        try:
            frame = await self._get_activity_frame(timeout=2)
            if frame:
                wrap = await self._get_active_wrap(frame)
                if wrap:
                    txt = (await wrap.inner_text()) or ""
                    low = txt.lower()
                    if (
                        "you scored" in low
                        or "you have scored" in low
                        or "your score" in low
                    ):
                        return True
                    if "out of" in low and "score" in low:
                        return True
            body = await self.page.inner_text("body")
            low = body.lower()
            if "next activity" in low and "start again" in low:
                return True
        except Exception:
            pass
        return False

    async def _fill_questions_on_page(self, page_questions):

        if not page_questions:
            return 0

        filled = 0
        mc_questions = [q for q in page_questions if q.get("type") == "Multiple Choice"]
        te_questions = [q for q in page_questions if q.get("type") == "Text Entry"]
        dd_questions = [q for q in page_questions if q.get("type") == "Dropdown"]
        drag_questions = [
            q
            for q in page_questions
            if q.get("type")
            in ("Drag & Drop", "Drag Drop", "Drag-and-Drop", "Gap Match")
        ]

        for idx, q in enumerate(mc_questions):
            if await self._fill_multiple_choice_for_current(q, idx):
                filled += 1
            await self.debug_wait(0.2)

        for idx, q in enumerate(te_questions):
            if await self._fill_text_entry_for_current(q, idx):
                filled += 1
            await self.debug_wait(0.2)

        for idx, q in enumerate(dd_questions):
            if await self._fill_dropdown_for_current(q, idx):
                filled += 1
            await self.debug_wait(0.2)

        for idx, q in enumerate(drag_questions):
            if q.get("correct_pairs"):
                if await self._fill_click_to_fill(q):
                    filled += 1
            else:
                if await self._fill_drag_drop_for_current(q, idx):
                    filled += 1
            await self.debug_wait(0.3)

        return filled

    async def continue_multi_question(
        self, exercise_name, answers_by_file, max_iterations=40
    ):
        print(f"\n  {Fore.CYAN}[*] เริ่มทำ Multi-Page ต่อจนจบ...{Style.RESET_ALL}")

        pages = []
        if answers_by_file:
            for filename, questions in answers_by_file.items():
                if questions:
                    pages.append((filename, questions))

        if not pages:
            print(f"  {Fore.YELLOW}[!] ไม่มีคำตอบในฐานข้อมูล{Style.RESET_ALL}")
            return False

        page_cursor = 0
        used_page_indexes = set()
        consecutive_failures = 0

        for iteration in range(1, max_iterations + 1):
            print(
                f"\n  {Fore.CYAN}[*] === รอบที่ {iteration}/{max_iterations} ==={Style.RESET_ALL}"
            )

            current_url = self.page.url
            if (
                "/view/" not in current_url
                and "/activity/" not in current_url
                and "item" not in current_url
            ):
                print(
                    f"  {Fore.GREEN}[+] ออกจากหน้าแบบฝึกหัดแล้ว — จบ{Style.RESET_ALL}"
                )
                return True

            if await self._is_result_screen():
                print(f"  {Fore.GREEN}[+] ถึงหน้าผลคะแนนแล้ว{Style.RESET_ALL}")
                return True

            frame = await self._get_activity_frame(timeout=10)
            if not frame:
                print(
                    f"  {Fore.YELLOW}[!] ไม่พบ activity frame — ลองใหม่รอบถัดไป{Style.RESET_ALL}"
                )
                await self.debug_wait(1)
                continue

            step = await self.get_current_question_number()
            page_qs = None
            page_name = ""

            if 1 <= step <= len(pages):
                page_name, page_qs = pages[step - 1]
                used_page_indexes.add(step - 1)
                print(
                    f"  {Fore.CYAN}[*] หน้าที่ {step}/{len(pages)} -> {page_name}{Style.RESET_ALL}"
                )
            else:
                while page_cursor < len(pages) and page_cursor in used_page_indexes:
                    page_cursor += 1
                if page_cursor < len(pages):
                    page_name, page_qs = pages[page_cursor]
                    used_page_indexes.add(page_cursor)
                    page_cursor += 1
                    print(
                        f"  {Fore.YELLOW}[!] อ่านเลขหน้าไม่ได้ (step={step}) — ใช้ลำดับถัดไป: {page_name}{Style.RESET_ALL}"
                    )

            if not page_qs:
                print(f"  {Fore.YELLOW}[!] ไม่มีเฉลยสำหรับหน้านี้แล้ว{Style.RESET_ALL}")
                if await self._is_result_screen():
                    return True

                if await self._click_next_button_if_present(timeout=5):
                    await self.debug_wait(2)
                    continue
                return False

            type_counts = {}
            for q in page_qs:
                t = q.get("type", "Unknown")
                type_counts[t] = type_counts.get(t, 0) + 1
            print(f"  {Fore.CYAN}[*] หน้านี้มี: {type_counts}{Style.RESET_ALL}")

            filled = await self._fill_questions_on_page(page_qs)
            print(f"  {Fore.CYAN}[*] เติมได้ {filled} ช่อง{Style.RESET_ALL}")
            if filled == 0:
                consecutive_failures += 1
                print(
                    f"  {Fore.YELLOW}[!] เติมคำตอบในหน้านี้ไม่ได้เลย (ต่อเนื่อง {consecutive_failures} ครั้ง){Style.RESET_ALL}"
                )
                if consecutive_failures >= 3:
                    return False

                if await self._click_next_button_if_present(timeout=5):
                    await self.debug_wait(2)
                    continue
                return False
            else:
                consecutive_failures = 0

            await self.debug_wait(0.8)
            print(f"  {Fore.CYAN}[*] กด Check...{Style.RESET_ALL}")
            checked = await self._click_check_button()
            if not checked:
                print(
                    f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check — อาจจบแล้ว{Style.RESET_ALL}"
                )
                if await self._is_result_screen():
                    return True
                return False

            await self.debug_wait(2.5)

            if await self._is_result_screen():
                print(
                    f"  {Fore.GREEN}[+] ทำครบทุกหน้าแล้ว (ผลคะแนนขึ้นแล้ว){Style.RESET_ALL}"
                )
                return True

            print(f"  {Fore.CYAN}[*] กดปุ่ม Next (หลัง Check)...{Style.RESET_ALL}")
            next_clicked = await self._click_next_button_if_present(timeout=8)
            if not next_clicked:
                print(
                    f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Next — อาจเป็นข้อสุดท้าย{Style.RESET_ALL}"
                )
                return await self._is_result_screen()

            expected_next = step + 1 if step > 0 else None
            changed = await self.wait_for_next_question(
                timeout=15, expected_next_num=expected_next
            )
            if not changed:
                new_step = await self.get_current_question_number()
                if step > 0 and new_step == step:
                    print(
                        f"  {Fore.YELLOW}[!] หน้าไม่เปลี่ยน (ยังอยู่ step {step}){Style.RESET_ALL}"
                    )
                    if await self._is_result_screen():
                        return True
                    return False
            print(f"  {Fore.GREEN}[+] ไปหน้าถัดไปแล้ว{Style.RESET_ALL}")
            await self.debug_wait(1.5)

        print(
            f"  {Fore.GREEN}[+] ทำ Multi-Page จบแล้ว ({iteration} รอบ){Style.RESET_ALL}"
        )
        return True

    async def handle_speaking_activity(self):

        if not await self._detect_speaking_ui():
            return False

        print(f"\n  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
        print(
            f"  {Fore.CYAN}[*] ตรวจพบ Speaking Activity — เริ่มอัดเสียงอัตโนมัติ{Style.RESET_ALL}"
        )
        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        max_turns = 30
        skipped_turns = 0
        for turn in range(1, max_turns + 1):
            print(
                f"\n  {Fore.CYAN}[*] === Speaking turn {turn}/{max_turns} ==={Style.RESET_ALL}"
            )

            if await self._is_result_screen():
                print(f"  {Fore.GREEN}[+] ถึงหน้าผลคะแนนแล้ว{Style.RESET_ALL}")
                return True

            record_btn = await self._find_recording_button()
            if not record_btn:
                await self.debug_wait(3)
                record_btn = await self._find_recording_button()
                if not record_btn:
                    print(
                        f"  {Fore.GREEN}[+] ไม่มีปุ่มอัดเสียงแล้ว — จบบทสนทนา{Style.RESET_ALL}"
                    )
                    return True

            label = ""
            try:
                label = (await record_btn.inner_text() or "").strip().lower()
            except Exception:
                pass
            print(f"  {Fore.CYAN}[*] ปุ่มอัดเสียง: '{label}'{Style.RESET_ALL}")

            if "try again" in label:
                print(
                    f"  {Fore.CYAN}[*] ปุ่มเป็น 'Try again' — ข้ามการอัด รอ Skip{Style.RESET_ALL}"
                )
                await self.debug_wait(2)
                outcome, skip_btn = await self._wait_for_speaking_outcome(timeout=45)
                print(f"  {Fore.CYAN}[*] ผลของท่อนนี้: {outcome}{Style.RESET_ALL}")
                if outcome == "done":
                    print(
                        f"  {Fore.GREEN}[+] อัดเสียงครบและขึ้นผลคะแนนแล้ว{Style.RESET_ALL}"
                    )
                    return True
                if outcome == "skip" and skip_btn:
                    try:
                        await skip_btn.scroll_into_view_if_needed()
                        await skip_btn.click(force=True)
                        skipped_turns += 1
                        print(
                            f"  {Fore.YELLOW}[+] ข้ามท่อนนี้ด้วยปุ่ม Skip (ครั้งที่ {skipped_turns}){Style.RESET_ALL}"
                        )
                        await self.debug_wait(3)
                        continue
                    except Exception as e:
                        print(
                            f"  {Fore.YELLOW}[!] กด Skip ไม่สำเร็จ: {e}{Style.RESET_ALL}"
                        )
                if outcome == "advanced":
                    print(f"  {Fore.GREEN}[+] ระบบไปท่อนถัดไปแล้ว{Style.RESET_ALL}")
                    await self.debug_wait(2)
                    continue
                print(f"  {Fore.YELLOW}[!] จบการอัดเสียงท่อนนี้{Style.RESET_ALL}")
                return True

            try:
                await record_btn.scroll_into_view_if_needed()
                await record_btn.click(force=True)
            except Exception as e:
                print(f"  {Fore.RED}[!] กดปุ่มอัดเสียงไม่สำเร็จ: {e}{Style.RESET_ALL}")
                return False

            await self.debug_wait(1.5)

            stop_btn = await self._wait_for_stop_button(timeout=10)
            if stop_btn:
                await self.debug_wait(3)
                try:
                    await stop_btn.click(force=True)
                    print(
                        f"  {Fore.CYAN}[*] หยุดอัดเสียงแล้ว — รอระบบตรวจ{Style.RESET_ALL}"
                    )
                except Exception as e:
                    print(f"  {Fore.YELLOW}[!] กด Stop ไม่สำเร็จ: {e}{Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Stop{Style.RESET_ALL}")

            outcome, skip_btn = await self._wait_for_speaking_outcome(timeout=45)
            print(f"  {Fore.CYAN}[*] ผลของท่อนนี้: {outcome}{Style.RESET_ALL}")

            if outcome == "done":
                print(
                    f"  {Fore.GREEN}[+] อัดเสียงครบและขึ้นผลคะแนนแล้ว{Style.RESET_ALL}"
                )
                return True

            if outcome == "skip" and skip_btn:
                try:
                    await skip_btn.scroll_into_view_if_needed()
                    await skip_btn.click(force=True)
                    skipped_turns += 1
                    print(
                        f"  {Fore.YELLOW}[+] ข้ามท่อนนี้ด้วยปุ่ม Skip (ครั้งที่ {skipped_turns}){Style.RESET_ALL}"
                    )
                    await self.debug_wait(3)
                    continue
                except Exception as e:
                    print(f"  {Fore.YELLOW}[!] กด Skip ไม่สำเร็จ: {e}{Style.RESET_ALL}")

            if outcome == "advanced":
                print(f"  {Fore.GREEN}[+] ระบบไปท่อนถัดไปแล้ว{Style.RESET_ALL}")
                await self.debug_wait(2)
                continue

            print(f"  {Fore.YELLOW}[!] จบการอัดเสียงท่อนนี้{Style.RESET_ALL}")
            return True

        print(
            f"  {Fore.YELLOW}[!] ครบ {max_turns} ท่อนแล้ว — ถือว่าจบ{Style.RESET_ALL}"
        )
        return True

    async def _fill_multiple_choice_by_index(self, q, question_index):

        return await self._fill_multiple_choice_for_current(q, question_index)

    async def _fill_checkbox(self, q):

        q = dict(q)
        q["is_multiple_answers"] = True
        idx = max(0, int(q.get("question_number", 1)) - 1)
        return await self._fill_multiple_choice_for_current(q, idx)

    async def _fill_dropdown(self, q):

        idx = max(0, int(q.get("question_number", 1)) - 1)
        return await self._fill_dropdown_for_current(q, idx)

    async def _fill_text_entry(self, q, index=0):

        return await self._fill_text_entry_for_current(q, index)

    async def _fill_click_to_fill(self, q):

        pairs = q.get("correct_pairs", {})
        if not pairs:
            print(
                f"  {Fore.YELLOW}[!] ไม่มี correct_pairs สำหรับ Click-to-fill{Style.RESET_ALL}"
            )
            return False

        if await self._find_gap_match_frame() is not None:
            print(
                f"  {Fore.CYAN}[*] ตรวจพบ Gap Match — เติมด้วยการคลิกคำใน pool{Style.RESET_ALL}"
            )
            return await self._fill_gap_match(q, pairs)

        ordered_pairs = sorted(pairs.items(), key=lambda x: self._gap_sort_key(x[0]))
        success_count = 0

        print(
            f"  {Fore.CYAN}[*] กำลังคลิกคำตอบจำนวน {len(ordered_pairs)} ข้อ...{Style.RESET_ALL}"
        )

        for idx, (gap_id, answer_text_raw) in enumerate(ordered_pairs, 1):
            answer_text = self._normalize_text(answer_text_raw)

            success = await self._click_answer_button(answer_text, gap_id, idx)

            if success:
                success_count += 1
                print(
                    f"      {Fore.GREEN}[OK] คลิก '{answer_text}' สำเร็จ{Style.RESET_ALL}"
                )
            else:
                print(
                    f"  {Fore.YELLOW}[!] คลิก '{answer_text}' ไม่สำเร็จ{Style.RESET_ALL}"
                )

            await self.debug_wait(0.8)

        return success_count == len(ordered_pairs)

    def _gap_sort_key(self, gap_id):

        match = re.search(r"(\d+)\s*$", str(gap_id))
        if match:
            return (0, int(match.group(1)))
        return (1, str(gap_id))

    async def _find_gap_match_frame(self):

        for frame in [self.page] + list(self.page.frames):
            try:
                count = await frame.evaluate(
                    "() => document.querySelectorAll('.gap_match_gap_view').length"
                )
            except Exception:
                continue
            if count:
                return frame
        return None

    async def _read_gap_state(self, frame):

        return await frame.evaluate(
            """
            () => {
                const wraps = [...document.querySelectorAll('.content-wrap')]
                    .filter(w => !w.classList.contains('activity--hidden'));
                const root = wraps.length ? wraps[wraps.length - 1] : document.body;
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                return {
                    gaps: [...root.querySelectorAll('.gap_match_gap_view')].map((g, i) => ({
                        index: i,
                        text: norm(g.innerText),
                        aria: g.getAttribute('aria-label') || ''
                    })),
                    pool: [...root.querySelectorAll('ul.pool li.drop_area')].map((li, i) => ({
                        index: i,
                        text: norm(li.innerText)
                    }))
                };
            }
        """
        )

    async def _click_gap_slot(self, frame, index):

        gaps = await frame.query_selector_all(".gap_match_gap_view")
        if index >= len(gaps):
            return False
        gap = gaps[index]
        holder = (
            await gap.query_selector(".droppable")
            or await gap.query_selector(".drag_holder")
            or await gap.query_selector(".droppable__placeholder")
            or gap
        )
        await holder.scroll_into_view_if_needed()
        await holder.click()
        return True

    async def _click_pool_text(self, frame, text):

        target = self._normalize_text(text).lower()
        state = await self._read_gap_state(frame)
        pool_index = None
        for item in state["pool"]:
            if item["text"] and self._normalize_text(item["text"]).lower() == target:
                pool_index = item["index"]
                break
        if pool_index is None:
            return False

        items = await frame.query_selector_all("ul.pool li.drop_area")
        if pool_index >= len(items):
            return False
        li = items[pool_index]
        clickable = (
            await li.query_selector("button")
            or await li.query_selector(".drag_element")
            or li
        )
        await clickable.scroll_into_view_if_needed()
        await clickable.click()
        return True

    async def _fill_gap_match(self, q, pairs):

        frame = await self._find_gap_match_frame()
        if frame is None:
            return False

        ordered = sorted(pairs.items(), key=lambda kv: self._gap_sort_key(kv[0]))
        state = await self._read_gap_state(frame)
        gap_count = len(state["gaps"])
        if not gap_count:
            return False
        desired = [self._normalize_text(v) for _k, v in ordered][:gap_count]

        cleared = 0
        for _ in range(gap_count * 3):
            st = await self._read_gap_state(frame)
            filled = [g["index"] for g in st["gaps"] if g["text"]]
            if not filled:
                break
            await self._click_gap_slot(frame, filled[0])
            await self.debug_wait(0.6)
            cleared += 1
        if cleared:
            print(
                f"  {Fore.CYAN}[*] ล้างคำตอบ Drag & Drop เดิม {cleared} ช่อง{Style.RESET_ALL}"
            )

        success = 0
        for idx, answer in enumerate(desired):
            if not await self._click_pool_text(frame, answer):
                print(f"  {Fore.YELLOW}[!] ไม่พบ '{answer}' ใน pool{Style.RESET_ALL}")
                continue
            await self.debug_wait(0.8)
            st = await self._read_gap_state(frame)
            current = ""
            if idx < len(st["gaps"]):
                current = self._normalize_text(st["gaps"][idx]["text"])
            if current.lower() == answer.lower():
                success += 1
                print(
                    f"      {Fore.GREEN}[OK] ช่อง {idx + 1} = '{answer}'{Style.RESET_ALL}"
                )
            else:
                print(
                    f"  {Fore.YELLOW}[!] ช่อง {idx + 1} ได้ '{current}' แต่ต้องเป็น '{answer}'{Style.RESET_ALL}"
                )

        return success == len(desired) and bool(desired)

    async def _click_answer_button(self, answer_text, gap_id, order_num, max_retries=2):

        clean_text = answer_text.strip().lower()

        for attempt in range(max_retries):

            for frame in self.page.frames:
                try:
                    selector = (
                        f"xpath=//*[(self::button or self::div or self::li or self::span) "
                        f"and normalize-space(translate(text(), "
                        f'"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))="{clean_text}"]'
                    )
                    element = frame.locator(selector).first
                    if await element.count() > 0 and await element.is_visible():
                        await element.scroll_into_view_if_needed()
                        await element.click(force=True)
                        await self.debug_wait(0.4)
                        return True
                except Exception:
                    continue

            for frame in self.page.frames:
                try:
                    js_clicked = await frame.evaluate(
                        """
                        (targetText) => {
                            const cleanTarget = targetText.trim().toLowerCase();
                            const candidates = document.querySelectorAll(
                                '.drag_element, .om-textgap-element, .drop_area, li, button, .draggable__content'
                            );
                            for (const el of candidates) {
                                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                                if (text === cleanTarget || text.includes(cleanTarget)) {
                                    const clickable = el.querySelector('button') || el;
                                    clickable.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                    clickable.click();
                                    clickable.dispatchEvent(new MouseEvent('click', {
                                        bubbles: true, cancelable: true, view: window
                                    }));
                                    return true;
                                }
                            }
                            return false;
                        }
                    """,
                        answer_text,
                    )
                    if js_clicked:
                        await self.debug_wait(0.4)
                        return True
                except Exception:
                    continue

            if attempt < max_retries - 1:
                await self.debug_wait(0.5)

        return False

    async def _click_check_button(self):
        print(f"  {Fore.CYAN}[*] กำลังหาปุ่ม Check...{Style.RESET_ALL}")

        check_selectors = [
            'a.green-btn[title="Check"]',
            'button.green-btn[title="Check"]',
            'a[title="Check"]',
            'button[title="Check"]',
            '[aria-label="Check"]',
            '[data-action="check"]',
            '[data-event="check"]',
            ".check-button",
            ".check-btn",
            ".activity-check-button",
            "a.green-btn",
            "a.btn.green-btn",
            "div.green-btn",
            'a:has-text("Check")',
            'button:has-text("Check")',
            'button:has-text("Submit")',
            'a:has-text("Submit")',
            ".submit-btn",
            'button[type="submit"]',
            '[data-action="submit"]',
            ".nemo-button-primary",
        ]

        for attempt in range(30):
            frames_to_search = [self.page] + list(self.page.frames)
            for frame in frames_to_search:
                for selector in check_selectors:
                    try:
                        buttons = await frame.query_selector_all(selector)
                        for btn in buttons:
                            if not await btn.is_visible():
                                continue

                            btn_text = (await btn.inner_text()).strip()
                            btn_title = (await btn.get_attribute("title") or "").strip()
                            aria_label = (
                                await btn.get_attribute("aria-label") or ""
                            ).strip()
                            action = (
                                (await btn.get_attribute("data-action") or "")
                                .strip()
                                .lower()
                            )
                            event = (
                                (await btn.get_attribute("data-event") or "")
                                .strip()
                                .lower()
                            )
                            class_attr = (
                                await btn.get_attribute("class") or ""
                            ).lower()

                            semantic_check = any(
                                "check" in value.lower() or "submit" in value.lower()
                                for value in (
                                    btn_text,
                                    btn_title,
                                    aria_label,
                                    action,
                                    event,
                                )
                                if value
                            )
                            class_only_selector = selector in {
                                ".check-button",
                                ".check-btn",
                                ".activity-check-button",
                                ".submit-btn",
                                'button[type="submit"]',
                            }
                            if not semantic_check and not class_only_selector:
                                continue

                            disabled_attr = await btn.get_attribute("disabled")
                            aria_disabled = (
                                await btn.get_attribute("aria-disabled") or ""
                            ).lower()
                            is_disabled = (
                                disabled_attr is not None
                                or aria_disabled == "true"
                                or "disabled" in class_attr
                                or "inactive" in class_attr
                            )
                            if is_disabled:
                                continue

                            label = btn_text or aria_label or btn_title or selector
                            print(
                                f"  {Fore.GREEN}[+] พบปุ่ม '{label}' - กำลังกด...{Style.RESET_ALL}"
                            )
                            await btn.scroll_into_view_if_needed()
                            await btn.click(force=True)
                            await self.debug_wait(2)
                            return True
                    except Exception:
                        continue

            try:
                js_clicked = await self.page.evaluate(
                    """
                    () => {
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const btns = [...document.querySelectorAll('button, a, [role="button"]')];
                        for (const b of btns) {
                            const t = norm(b.innerText);
                            if ((t === 'check' || t === 'check answers' || t === 'checkanswer')
                                && (b.offsetWidth || b.offsetHeight)
                                && !/inactive|disabled/.test((b.className || '').toString())) {
                                b.scrollIntoView({block: 'center'});
                                b.click();
                                return t;
                            }
                        }
                        return null;
                    }
                """
                )
                if js_clicked:
                    print(
                        f"  {Fore.GREEN}[+] พบปุ่ม Check (via JS) - กำลังกด...{Style.RESET_ALL}"
                    )
                    await self.debug_wait(2)
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.5)

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check{Style.RESET_ALL}")
        return False

    async def _read_marks(self):

        found = []
        frames = [self.page] + list(self.page.frames)
        for frame in frames:
            try:
                marks = await frame.evaluate(
                    """
                    () => {
                        const res = [];
                        document.querySelectorAll('[class*="check"]').forEach(el => {
                            const cls = (el.className || '').toString();
                            if (!/correct|incorrect|wrong/i.test(cls)) return;
                            res.push({tag: el.tagName, cls: cls.slice(0, 90),
                                      text: (el.innerText || '').trim().slice(0, 30)});
                        });
                        return res.slice(0, 60);
                    }
                """
                )
                if marks:
                    found.extend(marks)
            except Exception:
                continue
        return found

    def _is_wrong_mark(self, mark):
        return bool(mark.get("cls") and _WRONG_MARK_RE.search(mark["cls"]))

    def _is_correct_mark(self, mark):
        return bool(mark.get("cls") and _CORRECT_MARK_RE.search(mark["cls"]))

    async def _start_again_activity(self):

        for frame in [self.page] + list(self.page.frames):
            try:
                clicked = await frame.evaluate(
                    """
                    () => {
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const btns = [...document.querySelectorAll('button, a, [role="button"]')];
                        const hit = btns.find(b => norm(b.innerText) === 'start again')
                                 || btns.find(b => norm(b.innerText) === 'reset')
                                 || btns.find(b => norm(b.innerText) === 'try again');
                        if (!hit) return null;
                        hit.scrollIntoView({block: 'center'});
                        hit.click();
                        return norm(hit.innerText);
                    }
                """
                )
                if clicked:
                    print(
                        f"  {Fore.GREEN}[+] กด '{clicked}' สำหรับเริ่มแบบฝึกหัดใหม่{Style.RESET_ALL}"
                    )
                    await self.debug_wait(2)
                    return True
            except Exception:
                continue
        return False

    async def check_and_retry_if_needed(self, exercise_name, answers_by_file):
        print(f"\n  {Fore.CYAN}[*] กำลังตรวจสอบผลลัพธ์...{Style.RESET_ALL}")
        await self.debug_wait(2)

        try:

            body = await self.page.inner_text("body")
            body_lower = body.lower()

            marks = await self._read_marks()
            wrongs = [m for m in marks if self._is_wrong_mark(m)]
            corrects = [m for m in marks if self._is_correct_mark(m)]

            if wrongs:
                print(
                    f"  {Fore.YELLOW}[!] ตรวจพบคำตอบผิด {len(wrongs)} ข้อ — เริ่มแบบฝึกหัดใหม่{Style.RESET_ALL}"
                )
                started = await self._start_again_activity()
                if not started:
                    print(
                        f"  {Fore.YELLOW}[!] ไม่พบปุ่มเริ่มใหม่ (Start again / Reset){Style.RESET_ALL}"
                    )
                    return False

                await self.debug_wait(2)

                print(f"  {Fore.CYAN}[*] กำลังเติมคำตอบใหม่...{Style.RESET_ALL}")
                await self.auto_fill_and_check(exercise_name, answers_by_file)
                return True

            if corrects:
                print(
                    f"  {Fore.GREEN}[+] ตรวจพบคำตอบถูกครบถ้วน ({len(corrects)} ข้อ){Style.RESET_ALL}"
                )
                return True

            if "you scored" in body_lower:
                m = re.search(r"you scored\s+(\d+)\s+out of\s+(\d+)", body_lower)
                if m and int(m.group(1)) == int(m.group(2)):
                    print(
                        f"  {Fore.GREEN}[+] คะแนนเต็ม ({m.group(1)}/{m.group(2)}){Style.RESET_ALL}"
                    )
                    return True
                if m:
                    print(
                        f"  {Fore.YELLOW}[!] คะแนนไม่เต็ม ({m.group(1)}/{m.group(2)}) — เริ่มใหม่{Style.RESET_ALL}"
                    )
                    started = await self._start_again_activity()
                    if started:
                        await self.debug_wait(2)
                        await self.auto_fill_and_check(exercise_name, answers_by_file)
                    return True

            print(
                f"  {Fore.YELLOW}[!] ยังไม่พบเครื่องหมายถูก/ผิด — ถือว่าผ่านไปก่อน{Style.RESET_ALL}"
            )
            return False

        except Exception as e:
            print(
                f"  {Fore.RED}[!] check_and_retry_if_needed error: {e}{Style.RESET_ALL}"
            )
            pass

        return False

    async def _get_score_from_page(self):

        try:
            body = await self.page.inner_text("body")
            m = re.search(r"You scored\s+(\d+)\s+out of\s+(\d+)", body, re.IGNORECASE)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass

        try:
            marks = await self._read_marks()
            corrects = [m for m in marks if self._is_correct_mark(m)]
            wrongs = [m for m in marks if self._is_wrong_mark(m)]
            total = len(corrects) + len(wrongs)
            if total > 0:
                return len(corrects), total
        except Exception:
            pass

        return None, None

    async def retry_exercise(self, exercise_name):
        print(
            f"\n  {Fore.CYAN}[*] กำลังลอง Retry แบบฝึกหัด: {exercise_name}{Style.RESET_ALL}"
        )

        retry_selectors = [
            'button:has-text("Try again")',
            'button:has-text("Retry")',
            'button:has-text("Try Again")',
            'a:has-text("Try again")',
            'a:has-text("Retry")',
            ".retry-btn",
            ".try-again",
            '[data-action="retry"]',
            'button[data-event="retry"]',
        ]

        for selector in retry_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip()
                    print(
                        f"  {Fore.GREEN}[+] พบปุ่ม '{btn_text}' - กำลังกด...{Style.RESET_ALL}"
                    )
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await self.debug_wait(2)
                    return True
            except Exception:
                continue

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Retry{Style.RESET_ALL}")
        return False

    async def reset_exercise(self, exercise_name):
        print(
            f"\n  {Fore.CYAN}[*] กำลัง Reset แบบฝึกหัด: {exercise_name}{Style.RESET_ALL}"
        )

        reset_selectors = [
            'button:has-text("Reset")',
            'button:has-text("Start again")',
            'a:has-text("Reset")',
            ".reset-btn",
            '[data-action="reset"]',
            'button[data-event="reset"]',
            'button[data-event="reset_all"]',
        ]

        for selector in reset_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip()
                    print(
                        f"  {Fore.GREEN}[+] พบปุ่ม '{btn_text}' - กำลังกด...{Style.RESET_ALL}"
                    )
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await self.debug_wait(2)
                    return True
            except Exception:
                continue

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Reset{Style.RESET_ALL}")
        return False

    async def submit_activity_and_handle_result(self):
        print(f"\n  {Fore.CYAN}[*] กำลังส่งงาน (กดปุ่ม Next)...{Style.RESET_ALL}")

        next_clicked = False
        next_selectors = [
            'a:has-text("Next")',
            'button:has-text("Next")',
            ".green-btn",
            '[qid="next-btn"]',
            "a.btn.green-btn",
            'button[data-event="forward"]',
        ]

        for selector in next_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click(force=True)
                    next_clicked = True
                    break
            except Exception:
                continue

        if not next_clicked:
            try:
                await self.page.evaluate(
                    """
                    () => {
                        const btns = document.querySelectorAll('a, button');
                        for (const b of btns) {
                            if ((b.innerText || '').trim().toLowerCase() === 'next') {
                                b.click();
                                return true;
                            }
                        }
                    }
                """
                )
            except Exception:
                pass

        await self.debug_wait(2)
        await self.safe_wait_for_load()

        score_text = ""
        try:
            body_text = await self.page.inner_text("body")
            match = re.search(
                r"You scored\s+(\d+\s+out of\s+\d+)", body_text, re.IGNORECASE
            )
            if match:
                score_text = match.group(0)
                print(
                    f"  {Fore.GREEN}[+] ส่งงานสำเร็จ! คะแนน: {score_text}{Style.RESET_ALL}"
                )
            else:
                print(f"  {Fore.GREEN}[+] ส่งงานเรียบร้อยแล้ว{Style.RESET_ALL}")
        except Exception:
            pass

        print(f"\n  {Fore.CYAN}{'=' * 50}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN} ทำแบบฝึกหัดเสร็จสมบูรณ์ ({score_text}){Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 50}{Style.RESET_ALL}")
        print(f"  [1] ทำข้อต่อไป (Next activity)")
        print(f"  [2] ทำข้อนี้ใหม่ (Start again)")
        print(f"  [3] กลับไปหน้าเลือกแบบฝึกหัด")
        print(f"  [4] แสดง log file path")
        print(f"  [0] ปิดโปรแกรม")

        try:
            choice = input(
                f"\n  {Fore.CYAN}[>] เลือกการกระทำ (0-4): {Style.RESET_ALL}"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            choice = "3"

        if choice == "1":
            print(f"  {Fore.CYAN}[*] กำลังไปยังข้อถัดไป...{Style.RESET_ALL}")
            try:
                await self.page.click(
                    'a[qid="resultScreen-1"], .btn-primary:has-text("Next activity")',
                    force=True,
                )
                await self.safe_wait_for_load()
                await self.debug_wait(2)

                new_ex_name = await self.page.evaluate(
                    """
                    () => {
                        const titleEl = document.querySelector('.activity-title, h1, .product-title, header span, .header-title');
                        return titleEl ? titleEl.innerText.trim() : '';
                    }
                """
                )
                return ("next", new_ex_name)
            except Exception:
                pass
            return ("next", None)

        elif choice == "2":
            print(f"  {Fore.CYAN}[*] กำลังเริ่มทำข้อนี้ใหม่...{Style.RESET_ALL}")
            try:
                await self.page.click(
                    'a[qid="resultScreen-2"], .tryAgain-btn', force=True
                )
                await self.safe_wait_for_load()
            except Exception:
                pass
            return ("retry", None)

        elif choice == "4":
            print(
                f"\n  {Fore.CYAN}Log file: {self.log_file_path or 'ยังไม่เริ่ม'}{Style.RESET_ALL}"
            )
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass
            return ("retry", None)

        elif choice == "0":
            print(f"  {Fore.CYAN}[*] กำลังปิดโปรแกรม...{Style.RESET_ALL}")
            return ("close", None)

        else:
            print(f"  {Fore.CYAN}[*] กำลังกลับสู่หน้าหลัก...{Style.RESET_ALL}")
            await self.page.go_back()
            await self.safe_wait_for_load()
            return ("back", None)

    async def submit_single_page_activity(self):
        print(
            f"  {Fore.CYAN}[*] กำลังส่งแบบฝึกหัด (กดปุ่ม Next / Next activity)...{Style.RESET_ALL}"
        )
        await self.debug_wait(1.5)

        for attempt in range(15):
            frames_to_search = [self.page] + list(self.page.frames)
            for frame in frames_to_search:
                try:
                    buttons = await frame.query_selector_all("a, button, div.green-btn")
                except Exception:
                    continue
                for btn in buttons:
                    try:
                        if not await btn.is_visible():
                            continue
                        txt = (await btn.inner_text() or "").strip()
                        title = (await btn.get_attribute("title") or "").strip()
                        if txt.lower() not in (
                            "next",
                            "next activity",
                        ) and title.lower() not in ("next", "next activity"):
                            continue
                        cls = (await btn.get_attribute("class") or "").lower()
                        if "disabled" in cls or "inactive" in cls:
                            continue
                        print(
                            f"  {Fore.GREEN}[+] พบปุ่มส่งงาน '{txt}' - กำลังกด...{Style.RESET_ALL}"
                        )
                        await btn.scroll_into_view_if_needed()
                        await btn.click(force=True)
                        await self.safe_wait_for_load()
                        await self.debug_wait(2)
                        return True
                    except Exception:
                        continue

            try:
                js_clicked = await self.page.evaluate(
                    """
                    () => {
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const btns = [...document.querySelectorAll('button, a, div.green-btn')];
                        for (const b of btns) {
                            const t = norm(b.innerText);
                            const title = norm(b.getAttribute('title') || '');
                            if ((t === 'next' || t === 'next activity' || title === 'next' || title === 'next activity')
                                && (b.offsetWidth || b.offsetHeight)
                                && !/inactive|disabled/.test((b.className || '').toString())) {
                                b.scrollIntoView({block: 'center'});
                                b.click();
                                return t;
                            }
                        }
                        return null;
                    }
                """
                )
                if js_clicked:
                    print(
                        f"  {Fore.GREEN}[+] พบปุ่มส่งงาน (via JS) '{js_clicked}' - กำลังกด...{Style.RESET_ALL}"
                    )
                    await self.safe_wait_for_load()
                    await self.debug_wait(2)
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.5)

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Next สำหรับส่งงาน{Style.RESET_ALL}")
        return False

    async def init_browser(self, profile_dir=None):
        self.playwright = await async_playwright().start()
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = Path(profile_dir or (self.profiles_dir / "default"))
        profile_dir.mkdir(parents=True, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir.resolve()),
            headless=False,
            viewport={"width": 1366, "height": 768},
            permissions=["microphone"],
            args=[
                "--start-maximized",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ],
        )
        self.browser = self.context.browser
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        self.page.on("response", self._on_response)
        self.active_profile_dir = profile_dir

    def _on_response(self, response):
        try:
            url = response.url
            if "data.js" in url:
                if url not in self.data_js_urls:
                    self.data_js_urls.append(url)
                    self.last_data_js_url = url
                    print(
                        f"  {Fore.MAGENTA}[DATA.JS] ดัก URL ได้: {url[:120]}{Style.RESET_ALL}"
                    )
                    try:
                        loop = asyncio.get_event_loop()
                        loop.create_task(self._fetch_data_js_content(url, response))
                    except Exception:
                        pass
        except Exception as e:
            print(f"  {Fore.RED}[DATA.JS] error: {e}{Style.RESET_ALL}")

    async def _fetch_data_js_content(self, url, response):
        try:
            body = await response.body()
            content = body.decode("utf-8", errors="replace")
            self.data_js_contents[url] = content
            print(
                f"  {Fore.MAGENTA}[DATA.JS] ดาวน์โหลด content สำเร็จ: {len(content)} bytes{Style.RESET_ALL}"
            )
        except Exception as e:
            print(f"  {Fore.YELLOW}[DATA.JS] โหลด content ไม่ได้: {e}{Style.RESET_ALL}")

    async def _wait_for_data_js(self, timeout=20):
        if self.data_js_urls:
            all_have_content = all(
                u in self.data_js_contents for u in self.data_js_urls
            )
            if all_have_content:
                print(
                    f"  {Fore.GREEN}[DATA.JS] มีข้อมูลพร้อมใช้งานอยู่แล้ว: {len(self.data_js_urls)} URL{Style.RESET_ALL}"
                )
                return True

        print(
            f"  {Fore.CYAN}[DATA.JS] เริ่มรอ data.js (timeout={timeout}s)...{Style.RESET_ALL}"
        )
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            if self.data_js_urls:
                all_have_content = all(
                    u in self.data_js_contents for u in self.data_js_urls
                )
                if all_have_content:
                    print(
                        f"  {Fore.GREEN}[DATA.JS] พร้อมใช้งาน: {len(self.data_js_urls)} URL{Style.RESET_ALL}"
                    )
                    return True
            await asyncio.sleep(0.3)

        if self.data_js_urls:
            print(
                f"  {Fore.YELLOW}[DATA.JS] timeout แต่มี URL อยู่ {len(self.data_js_urls)}{Style.RESET_ALL}"
            )
            return True
        print(f"  {Fore.RED}[DATA.JS] timeout — ไม่พบ data.js{Style.RESET_ALL}")
        return False

    async def check_login_status(self):
        print(f"  {Fore.CYAN}[*] กำลังตรวจสอบ session...{Style.RESET_ALL}")
        try:
            await self.page.goto(
                "https://www.cambridgeone.org/dashboard/learner/dashboard",
                wait_until="domcontentloaded",
                timeout=20000,
            )
        except Exception:
            pass

        await self.debug_wait(2)

        try:
            await self.page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass

        current_url = self.page.url.lower()
        print(f"  {Fore.CYAN}[*] URL ปัจจุบัน: {current_url}{Style.RESET_ALL}")

        if "/login" in current_url:
            print(f"  {Fore.YELLOW}[!] Session หมดอายุ{Style.RESET_ALL}")
            return False

        try:
            login_form = await self.page.query_selector(
                'input[type="password"], input[name="password"], .gigya-input-password'
            )
            if login_form and await login_form.is_visible():
                print(
                    f"  {Fore.YELLOW}[!] Session หมดอายุ (พบ form login){Style.RESET_ALL}"
                )
                return False
        except Exception:
            pass

        if "dashboard" in current_url or "/learner/" in current_url:
            try:
                body = await self.page.inner_text("body")
                body_lower = body.lower()
                if "my classes" in body_lower or "active classes" in body_lower:
                    print(f"  {Fore.GREEN}[+] Session ยังใช้งานได้{Style.RESET_ALL}")
                    return True
            except Exception:
                pass
            print(f"  {Fore.GREEN}[+] Session ยังใช้งานได้{Style.RESET_ALL}")
            return True

        return False

    async def login(self, email, password, _attempt=0):
        print(f"\n  {Fore.CYAN}[*] Logging in...{Style.RESET_ALL}")
        await self.page.goto("https://www.cambridgeone.org/login")
        await self.safe_wait_for_load()
        await self.debug_wait(1)

        email_field = None
        email_selectors = [
            'input[type="text"][name="username"]',
            'input[type="text"][name="loginID"]',
            'input[type="email"]',
            "input.gigya-input-text",
            'input[data-gigya-name="loginID"]',
            'input[id*="loginID"]',
            'input[id*="login"]',
            'input[placeholder*="username"]',
            'input[placeholder*="email"]',
            'input[placeholder*="Username"]',
            'input[placeholder*="Email"]',
        ]

        for selector in email_selectors:
            try:
                email_field = await self.page.query_selector(selector)
                if (
                    email_field
                    and await email_field.is_visible()
                    and await email_field.is_enabled()
                ):
                    break
                email_field = None
            except Exception:
                email_field = None
                continue

        if not email_field:
            print(f"  {Fore.RED}[-] Could not find email field{Style.RESET_ALL}")
            if _attempt < 1:
                await self.debug_wait(3)
                return await self.login(email, password, _attempt + 1)
            return False

        await email_field.click()
        await email_field.fill("")
        await email_field.type(email, delay=20)
        await email_field.press("Tab")
        await self.debug_wait(0.5)

        password_field = None
        password_selectors = [
            'input[type="password"]',
            "input.gigya-input-password",
            'input[data-gigya-name="password"]',
            'input[id*="password"]',
            'input[name="password"]',
            'input[placeholder*="password"]',
            'input[placeholder*="Password"]',
        ]

        for selector in password_selectors:
            try:
                password_field = await self.page.query_selector(selector)
                if (
                    password_field
                    and await password_field.is_visible()
                    and await password_field.is_enabled()
                ):
                    break
                password_field = None
            except Exception:
                password_field = None
                continue

        if not password_field:
            print(f"  {Fore.RED}[-] Could not find password field{Style.RESET_ALL}")
            if _attempt < 1:
                await self.debug_wait(3)
                return await self.login(email, password, _attempt + 1)
            return False

        await password_field.click()
        await password_field.fill("")
        await password_field.type(password, delay=20)
        await password_field.press("Enter")
        await self.debug_wait(3)

        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        current_url = self.page.url
        if (
            "dashboard" in current_url
            or "home" in current_url
            or "classes" in current_url
        ):
            print(f"  {Fore.GREEN}[+] Login successful!{Style.RESET_ALL}")
            return True

        try:
            page_text = await self.page.inner_text("body")
            if "Active classes" in page_text or "My classes" in page_text:
                print(f"  {Fore.GREEN}[+] Login successful!{Style.RESET_ALL}")
                return True
        except Exception:
            pass

        print(f"  {Fore.RED}[-] Login failed{Style.RESET_ALL}")
        if _attempt < 1:
            await self.debug_wait(3)
            return await self.login(email, password, _attempt + 1)
        return False

    async def get_courses_with_workbooks(self):
        print(
            f"\n  {Fore.CYAN}[*] Fetching courses with Digital Workbooks...{Style.RESET_ALL}"
        )
        current_url = self.page.url
        if "dashboard" not in current_url:
            await self.page.goto(
                "https://www.cambridgeone.org/dashboard/learner/dashboard"
            )
            await self.safe_wait_for_load()
            await self.debug_wait(1)

        courses_data = []
        for attempt in range(3):
            try:
                await self.page.wait_for_selector(".umbrella-tile", timeout=15000)
            except Exception:
                pass
            try:
                courses_data = await self.page.evaluate(
                    """
                    () => {
                        const courses = [];
                        const tiles = document.querySelectorAll('.umbrella-tile');
                        tiles.forEach(tile => {
                            const link = tile.querySelector('a.tile-section-link');
                            if (!link) return;
                            const ariaLabel = link.getAttribute('aria-label') || '';
                            if (!ariaLabel.includes('Digital Workbook')) return;
                            const nameElem = tile.querySelector('.my-progress');
                            const workbookName = nameElem ? nameElem.innerText.trim() : '';
                            let courseName = '';
                            const match = ariaLabel.match(/for\\s+([^\\-]+?)(?:\\s*[–-]|$)/);
                            if (match) courseName = match[1].trim();
                            else {
                                const allText = tile.innerText || '';
                                const lines = allText.split('\\n').filter(line => line.trim());
                                for (const line of lines) {
                                    if (line.includes('Four Corners') || line.includes('Cambridge')) {
                                        courseName = line.trim();
                                        break;
                                    }
                                }
                            }
                            if (!courseName) {
                                const parts = ariaLabel.split(' for ');
                                if (parts.length > 1) courseName = parts[1].trim();
                            }
                            if (workbookName && courseName) courses.push({ courseName, workbookName });
                        });
                        return courses;
                    }
                """
                )
            except Exception:
                courses_data = []

            if courses_data:
                break
            if attempt < 2:
                print(
                    f"  {Fore.YELLOW}[!] ยังโหลดคอร์สไม่ได้ — ลองใหม่ ({attempt + 2}/3){Style.RESET_ALL}"
                )
                try:
                    await self.page.reload()
                    await self.safe_wait_for_load()
                except Exception:
                    pass
                await self.debug_wait(3)

        print(f"  {Fore.GREEN}[+] พบ {len(courses_data)} คอร์ส{Style.RESET_ALL}")
        return courses_data

    async def click_workbook(self, course_name):
        print(
            f"  {Fore.CYAN}[*] กำลังเปิด Digital Workbook: {course_name}{Style.RESET_ALL}"
        )
        result = await self.page.evaluate(
            f"""
            (courseName) => {{
                const tiles = document.querySelectorAll('.umbrella-tile');
                for (const tile of tiles) {{
                    const link = tile.querySelector('a.tile-section-link');
                    if (!link) continue;
                    const ariaLabel = link.getAttribute('aria-label') || '';
                    if (!ariaLabel.includes('Digital Workbook')) continue;
                    if (ariaLabel.includes(courseName)) {{ link.click(); return true; }}
                    const text = tile.innerText || '';
                    if (text.includes(courseName) && ariaLabel.includes('Digital Workbook')) {{ link.click(); return true; }}
                }}
                return false;
            }}
        """,
            course_name,
        )
        if result:
            print(f"  {Fore.GREEN}[+] คลิก Digital Workbook สำเร็จ{Style.RESET_ALL}")
            await self.safe_wait_for_load()
            await self.wait_for_loading_spinner(timeout=10)
            await self.debug_wait(2)
            return True
        print(f"  {Fore.RED}[-] ไม่พบ Digital Workbook{Style.RESET_ALL}")
        return False

    async def get_all_units(self):
        print(f"\n  {Fore.CYAN}[*] กำลังโหลด Units...{Style.RESET_ALL}")

        for attempt in range(4):
            try:
                await self.wait_for_loading_spinner(timeout=8)
                await self.page.wait_for_selector(
                    '.card, .unit-detail, [class*="unit"]', timeout=5000
                )
                break
            except Exception:
                await asyncio.sleep(2)

        units_data = []
        for retry in range(3):
            units_data = await self.page.evaluate(
                """
                () => {
                    const units = [];
                    const cards = document.querySelectorAll('.card');
                    cards.forEach((card) => {
                        let unitName = '';
                        const h5 = card.querySelector('.unit-info .h5');
                        if (h5) unitName = h5.innerText.trim();
                        if (!unitName) {
                            const h5_2 = card.querySelector('.unit-detail .h5');
                            if (h5_2) unitName = h5_2.innerText.trim();
                        }
                        if (!unitName) {
                            const lines = (card.innerText || '').split('\\n').filter(line => line.trim());
                            for (const line of lines) {
                                if (line.match(/^\\d+\\s+\\w+/)) { unitName = line.trim(); break; }
                            }
                        }
                        if (unitName) units.push({ name: unitName });
                    });

                    if (units.length === 0) {
                        document.querySelectorAll('.unit-detail, [class*="unit-title"]').forEach(detail => {
                            const h5 = detail.querySelector('.h5, h5, .title');
                            if (h5) {
                                const name = h5.innerText.trim();
                                if (name) units.push({ name });
                            } else {
                                const txt = detail.innerText.split('\\n')[0].trim();
                                if (txt) units.push({ name: txt });
                            }
                        });
                    }
                    return units;
                }
            """
            )
            if units_data:
                break
            await asyncio.sleep(2)

        print(f"  {Fore.GREEN}[+] พบ {len(units_data)} บทเรียน{Style.RESET_ALL}")
        return units_data

    async def click_unit(self, unit_name):
        print(f"  {Fore.CYAN}[*] กำลังเปิดบท: {unit_name}{Style.RESET_ALL}")
        result = await self.page.evaluate(
            f"""
            (unitName) => {{
                let targetCard = null;
                for (const card of document.querySelectorAll('.card, [class*="unit"]')) {{
                    if ((card.innerText || '').includes(unitName)) {{ targetCard = card; break; }}
                }}
                if (!targetCard) return false;
                const collapse = targetCard.querySelector('.collapse');
                if (collapse && collapse.classList.contains('show')) return true;
                const clickable = targetCard.querySelector('.unit-detail, .card-header, a, button, h5');
                if (clickable) {{ clickable.click(); return true; }}
                return false;
            }}
        """,
            unit_name,
        )
        if result:
            print(f"  {Fore.GREEN}[+] เปิดบทสำเร็จ{Style.RESET_ALL}")
            await self.safe_wait_for_load()
            await self.debug_wait(1.5)
            return True
        print(f"  {Fore.RED}[-] ไม่พบบทนี้{Style.RESET_ALL}")
        return False

    async def get_exercises_in_unit(self, unit_name):
        print(f"\n  {Fore.CYAN}[*] Fetching exercises in: {unit_name}{Style.RESET_ALL}")
        try:
            await self.page.wait_for_selector(
                '.activity-info, [class*="activity"], [class*="lesson"], .card',
                timeout=8000,
            )
        except Exception:
            pass

        exercises_data = await self.page.evaluate(
            rf"""
            (unitName) => {{
                const result = {{ unit_name: unitName, lessons: [] }};

                let unitCard = null;
                const allCards = document.querySelectorAll('.card');
                for (const card of allCards) {{
                    const h5 = card.querySelector('.unit-info .h5, .unit-detail .h5');
                    const name = h5 ? h5.innerText.trim() : '';
                    if (name && name.toLowerCase() === unitName.toLowerCase()) {{
                        unitCard = card;
                        break;
                    }}
                }}
                if (!unitCard) {{
                    for (const card of allCards) {{
                        const cardText = (card.innerText || '').toLowerCase();
                        if (cardText.includes(unitName.toLowerCase())) {{
                            unitCard = card;
                            break;
                        }}
                    }}
                }}
                if (!unitCard) {{
                    return result;
                }}

                const cleanName = (raw) => {{
                    let name = (raw || '').replace(/\s+/g, ' ').trim();
                    name = name.replace(/^(Locked|Completed|In progress|Not started)\s*/i, '').trim();
                    return name;
                }};

                const statusOf = (a) => {{
                    if (a.querySelector('.nemo-tick, .lch-green-tick')) return 'completed';
                    if (a.querySelector('.lch-in-progress-icon')) return 'in-progress';
                    return 'pending';
                }};

                const lessonOf = (a) => {{
                    const collapse = a.closest('.collapse');
                    if (collapse) {{
                        const label = collapse.getAttribute('aria-label') || '';
                        const m = label.match(/Lesson\s*-\s*(.+)$/i);
                        if (m) return 'Lesson ' + m[1].trim();
                        if (/^Lesson\s+[A-Z]/i.test(label)) return label.trim();
                    }}
                    let node = a.closest('.activities-info');
                    while (node) {{
                        const prev = node.previousElementSibling;
                        if (prev) {{
                            const t = (prev.innerText || '').trim();
                            const m = t.match(/^(Lesson\s+[A-Z])/i);
                            if (m) return m[1].toUpperCase().replace('LESSON', 'Lesson');
                        }}
                        node = node.parentElement;
                    }}
                    return '';
                }};

                const getLesson = (name) => {{
                    let lesson = result.lessons.find(l => l.name.toLowerCase() === name.toLowerCase());
                    if (!lesson) {{
                        lesson = {{ name: name || 'Lesson A', exercises: [] }};
                        result.lessons.push(lesson);
                    }}
                    return lesson;
                }};

                const activities = unitCard.querySelectorAll('a.activity-info');
                for (const a of activities) {{
                    const name = cleanName(
                        a.getAttribute('aria-label') ||
                        (a.querySelector('p[title]') ? a.querySelector('p[title]').getAttribute('title') : '') ||
                        a.innerText
                    );
                    if (!name || name.length < 2) continue;
                    if (/^Lesson\s+[A-Z]/i.test(name)) continue;
                    if (name.toLowerCase() === unitName.toLowerCase()) continue;
                    if (/^\d+\s+/.test(name)) continue;

                    const lesson = getLesson(lessonOf(a) || 'Lesson A');
                    if (!lesson.exercises.some(e => e.name.toLowerCase() === name.toLowerCase())) {{
                        lesson.exercises.push({{ name: name, status: statusOf(a) }});
                    }}
                }}

                if (result.lessons.length === 0 || result.lessons.every(l => l.exercises.length === 0)) {{
                    result.lessons = [];
                    const fallbackLesson = {{ name: 'Exercises', exercises: [] }};
                    unitCard.querySelectorAll('a, button, [role="button"]').forEach(item => {{
                        const rawText = (item.innerText || '').trim();
                        if (!rawText) return;
                        const name = cleanName(rawText.split('\n')[0]);
                        if (name && name.length > 2 &&
                            !name.toLowerCase().includes('lesson') &&
                            !name.toLowerCase().includes('unit') &&
                            name.toLowerCase() !== unitName.toLowerCase() &&
                            !/^\d+\s+/.test(name)) {{
                            if (!fallbackLesson.exercises.some(e => e.name === name)) {{
                                fallbackLesson.exercises.push({{ name: name, status: statusOf(item) }});
                            }}
                        }}
                    }});
                    if (fallbackLesson.exercises.length > 0) {{
                        result.lessons.push(fallbackLesson);
                    }}
                }}

                return result;
            }}
        """,
            unit_name,
        )

        print(
            f"  {Fore.GREEN}[+] พบ {len(exercises_data['lessons'])} Lesson{Style.RESET_ALL}"
        )
        return exercises_data

    async def click_exercise(self, exercise_name):
        print(f"  {Fore.CYAN}[*] กำลังเปิดแบบฝึกหัด: {exercise_name}{Style.RESET_ALL}")

        current_url = self.page.url
        if (
            "/view/" in current_url
            or "/activity/" in current_url
            or "item" in current_url
        ):
            current_title = await self.page.evaluate(
                """
                () => {
                    const el = document.querySelector('.activity-title, h1, header span, .product-title, .title');
                    return el ? el.innerText.trim().toLowerCase() : '';
                }
            """
            )
            if current_title and exercise_name.lower() in current_title:
                print(
                    f"  {Fore.GREEN}[+] อยู่ในหน้าแบบฝึกหัดข้อนี้เรียบร้อยแล้ว{Style.RESET_ALL}"
                )
                await self.debug_wait(3)
                return True

            print(f"  {Fore.CYAN}[*] กำลังย้อนกลับสู่หน้า Workbook...{Style.RESET_ALL}")
            await self.page.go_back()
            await self.safe_wait_for_load()
            await self.debug_wait(1)

        result = await self.page.evaluate(
            rf"""
            (exName) => {{
                const cleanName = exName.trim().toLowerCase();

                const activities = document.querySelectorAll('.activity-info, [class*="activity"]');
                for (const act of activities) {{
                    const textContent = (act.innerText || act.textContent || '').trim().toLowerCase();
                    if (textContent.includes(cleanName)) {{
                        const target = act.querySelector('a, button, [role="button"], p, span') || act;
                        target.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        target.click();
                        return true;
                    }}
                }}

                const allClickables = document.querySelectorAll('a, button, [role="button"]');
                for (const elem of allClickables) {{
                    const txt = (elem.innerText || elem.textContent || '').trim().toLowerCase();

                    if (elem.closest('.card-header') || elem.classList.contains('unit-detail')) continue;

                    if (txt === cleanName || (txt.includes(cleanName) && txt.length < cleanName.length + 20)) {{
                        elem.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        elem.click();
                        return true;
                    }}
                }}
                return false;
            }}
        """,
            exercise_name,
        )

        if result:
            print(f"  {Fore.GREEN}[+] เปิดแบบฝึกหัดสำเร็จ{Style.RESET_ALL}")
            await self.safe_wait_for_load()
            await self.wait_for_loading_spinner(timeout=10)
            await self.debug_wait(3)
            return True
        print(f"  {Fore.RED}[-] ไม่พบแบบฝึกหัดนี้{Style.RESET_ALL}")
        return False

    def _parse_js_object(self, content):
        print(
            f"  {Fore.CYAN}[PARSE] เริ่มแกะ data.js (ขนาด {len(content)} bytes)...{Style.RESET_ALL}"
        )
        m = re.search(r"ajaxData\s*=\s*", content)
        if not m:
            print(f"  {Fore.RED}[PARSE] ไม่พบ 'ajaxData ='{Style.RESET_ALL}")
            return None

        start_idx = m.end()
        decoder = json.JSONDecoder()
        try:
            data, end_idx = decoder.raw_decode(content, start_idx)
            print(
                f"  {Fore.GREEN}[PARSE] แกะ JSON สำเร็จ — keys: {len(data)}{Style.RESET_ALL}"
            )
            return data
        except json.JSONDecodeError as e:
            last_brace = content.rfind("}")
            if last_brace > start_idx:
                snippet = content[start_idx : last_brace + 1]
                try:
                    data = json.loads(snippet)
                    print(
                        f"  {Fore.GREEN}[PARSE] fallback json.loads สำเร็จ — keys: {len(data)}{Style.RESET_ALL}"
                    )
                    return data
                except Exception as e2:
                    print(
                        f"  {Fore.RED}[PARSE] fallback ล้มเหลว: {e2}{Style.RESET_ALL}"
                    )
                    return None
        except Exception as e:
            print(f"  {Fore.RED}[PARSE] error: {e}{Style.RESET_ALL}")
            return None

    def _decode_unicode_escapes(self, text):

        if not text:
            return ""
        res = text
        try:
            if "\\u" in res:
                res = re.sub(
                    r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), res
                )
            if "\\/" in res:
                res = res.replace("\\/", "/")
        except Exception:
            res = text
        res = self._normalize_text(res)
        return res

    def _normalize_text(self, text):
        if not text:
            return ""

        text = text.replace("â€™", "'").replace("â€œ", '"').replace("â€", '"')
        text = text.replace("â€“", "-").replace("â€˜", "'").replace("â", "'")

        replacements = {
            "\u2019": "'",
            "\u2018": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u2013": "-",
            "\u2014": "-",
            "\u2026": "...",
            "\u00a0": " ",
            "\u200b": "",
            "`": "'",
        }
        for k, v in replacements.items():
            text = text.replace(k, v)

        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_answers_from_xml(self, xml_content):
        try:
            root = ET.fromstring(xml_content)
        except Exception as e:
            print(f"  {Fore.RED}[XML] parse error: {e}{Style.RESET_ALL}")
            return []

        qti = "{http://www.imsglobal.org/xsd/imsqti_v2p1}"

        instruction = self._extract_rubric_text(root, qti)
        audio_filename = self._extract_audio_filename(root, qti)

        questions = []

        mc_interactions = root.findall(f".//{qti}choiceInteraction")
        if not mc_interactions:
            mc_interactions = root.findall(".//choiceInteraction")
        if mc_interactions:
            questions.extend(
                self._parse_multiple_choice(
                    root, qti, mc_interactions, instruction, audio_filename
                )
            )
            print(
                f"  {Fore.CYAN}[XML] Multiple Choice: {len(mc_interactions)} interaction(s){Style.RESET_ALL}"
            )

        dd_interactions = root.findall(f".//{qti}inlineChoiceInteraction")
        if not dd_interactions:
            dd_interactions = root.findall(".//inlineChoiceInteraction")
        if dd_interactions:
            questions.extend(
                self._parse_dropdown(
                    root, qti, dd_interactions, instruction, audio_filename
                )
            )
            print(
                f"  {Fore.CYAN}[XML] Dropdown: {len(dd_interactions)} interaction(s){Style.RESET_ALL}"
            )

        te_interactions = root.findall(f".//{qti}textEntryInteraction")
        if not te_interactions:
            te_interactions = root.findall(".//textEntryInteraction")
        if te_interactions:
            questions.extend(
                self._parse_text_entry(
                    root, qti, te_interactions, instruction, audio_filename
                )
            )
            print(
                f"  {Fore.CYAN}[XML] Text Entry: {len(te_interactions)} interaction(s){Style.RESET_ALL}"
            )

        gm_interactions = root.findall(f".//{qti}gapMatchInteraction")
        if not gm_interactions:
            gm_interactions = root.findall(".//gapMatchInteraction")
        if gm_interactions:
            questions.extend(
                self._parse_gap_match(
                    root, qti, gm_interactions, instruction, audio_filename
                )
            )
            print(
                f"  {Fore.CYAN}[XML] Gap Match: {len(gm_interactions)} interaction(s){Style.RESET_ALL}"
            )

        return questions

    def _extract_rubric_text(self, root, qti):
        rubric = root.find(f'.//{qti}div[@id="rubric"]')
        if rubric is None:
            rubric = root.find('.//div[@id="rubric"]')
        if rubric is None:
            return ""
        for p in rubric.findall(f".//{qti}p"):
            text = "".join(p.itertext()).strip()
            if text:
                return text
        for p in rubric.findall(".//p"):
            text = "".join(p.itertext()).strip()
            if text:
                return text
        return "".join(rubric.itertext()).strip()

    def _extract_audio_filename(self, root, qti):
        header_audio = root.find(f'.//{qti}div[@id="headerAudio"]')
        if header_audio is None:
            header_audio = root.find('.//div[@id="headerAudio"]')
        if header_audio is None:
            return ""
        data_author = header_audio.get("data-author", "") or ""
        try:
            data_author_clean = html_module.unescape(data_author)
            audio_info = json.loads(data_author_clean)
            return audio_info.get("mediaID", "")
        except Exception:
            m = re.search(r'"mediaID"\s*:\s*"([^"]+)"', data_author)
            return m.group(1) if m else ""

    def _parse_multiple_choice(
        self, root, qti, interactions, instruction, audio_filename
    ):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            answers = []
            correct_texts = []
            correct_ids = []
            choice_ids = []

            choices = interaction.findall(f".//{qti}simpleChoice")
            if not choices:
                choices = interaction.findall(".//simpleChoice")

            response_id = (interaction.get("responseIdentifier") or "").strip()

            response_decl = None
            for rd in root.findall(f".//{qti}responseDeclaration"):
                if (rd.get("identifier") or "").strip() == response_id:
                    response_decl = rd
                    break

            if response_decl is None:
                response_decl = root.find(f".//{qti}responseDeclaration")
            if response_decl is None:
                response_decl = root.find(".//responseDeclaration")

            if response_decl is not None:
                cr = response_decl.find(f".//{qti}correctResponse")
                if cr is None:
                    cr = response_decl.find(".//correctResponse")
                if cr is not None:
                    for v in cr.findall(f".//{qti}value"):
                        if v.text:
                            correct_ids.append(v.text.strip())
                    if not correct_ids:
                        for v in cr.findall(".//value"):
                            if v.text:
                                correct_ids.append(v.text.strip())

            if not correct_ids:
                for choice in choices:
                    feedback = choice.get("answerfeedback", "") or ""
                    if "#feedback:p1#" in feedback:
                        cid = (choice.get("identifier") or "").strip()
                        if cid:
                            correct_ids.append(cid)

            for choice in choices:
                choice_text = self._normalize_text("".join(choice.itertext()))
                if not choice_text:

                    alt_parts = []
                    for img in choice.iter():
                        if img.tag.lower().endswith("img"):
                            alt = img.get("alt") or ""
                            if alt.strip():
                                alt_parts.append(alt.strip())
                    choice_text = self._normalize_text(" ".join(alt_parts))
                answers.append(choice_text)

                choice_id = (choice.get("identifier") or "").strip()
                if choice_id:
                    choice_ids.append(choice_id)
                if choice_id in correct_ids:
                    correct_texts.append(choice_text)

            if not correct_texts:
                for choice in choices:
                    feedback = choice.get("answerfeedback", "") or ""
                    if "#feedback:p1#" in feedback:
                        choice_text = self._normalize_text("".join(choice.itertext()))
                        if not choice_text:
                            for img in choice.iter():
                                if img.tag.lower().endswith("img"):
                                    alt = img.get("alt") or ""
                                    if alt.strip():
                                        choice_text = self._normalize_text(alt)
                                        break
                        if choice_text:
                            correct_texts.append(choice_text)

            if answers:
                correct_index = -1
                if correct_ids:
                    for idx_c, cid in enumerate(choice_ids):
                        if cid in correct_ids:
                            correct_index = idx_c
                            break
                questions.append(
                    {
                        "type": "Multiple Choice",
                        "question_number": i,
                        "question": instruction or f"Question {i}",
                        "instruction": instruction,
                        "audio_filename": audio_filename,
                        "answers": answers,
                        "correct_answer": correct_texts[0] if correct_texts else "",
                        "correct_answers": correct_texts,
                        "is_multiple_answers": len(correct_texts) > 1,
                        "correct_id": correct_ids[0] if correct_ids else "",
                        "choice_ids": choice_ids,
                        "correct_index": correct_index,
                        "response_id": response_id,
                    }
                )
        return questions

    def _parse_dropdown(self, root, qti, interactions, instruction, audio_filename):
        questions = []

        response_map = {}
        for rd in root.findall(f".//{qti}responseDeclaration"):
            rid = (rd.get("identifier") or "").strip()
            if not rid:
                continue
            cr = rd.find(f".//{qti}correctResponse")
            if cr is None:
                cr = rd.find(".//correctResponse")
            if cr is not None:
                val = cr.find(f".//{qti}value")
                if val is None:
                    val = cr.find(".//value")
                if val is not None and val.text:
                    response_map[rid] = val.text.strip()

        print(
            f"  {Fore.CYAN}[DROPDOWN] พบ responseDeclaration {len(response_map)} ตัว{Style.RESET_ALL}"
        )

        fallback_answers = []
        for p in root.findall(f".//{qti}p"):
            txt = "".join(p.itertext()).strip()
            if "Correct Answer:" in txt or "Correct answer:" in txt:
                parts = txt.split("Correct Answer:")
                if len(parts) < 2:
                    parts = txt.split("Correct answer:")
                if len(parts) >= 2:
                    ans = self._normalize_text(parts[1].strip())
                    if ans:
                        fallback_answers.append(ans)

        contentblock = root.find(f'.//{qti}div[@id="contentblock"]')
        if contentblock is None:
            contentblock = root.find('.//div[@id="contentblock"]')

        for i, interaction in enumerate(interactions, 1):
            response_id = (interaction.get("responseIdentifier") or "").strip()

            options = []
            for choice in interaction.findall(f".//{qti}inlineChoice"):
                text = self._normalize_text("".join(choice.itertext()))
                cid = (choice.get("identifier") or "").strip()
                if text:
                    options.append((cid, text))
            if not options:
                for choice in interaction.findall(".//inlineChoice"):
                    text = self._normalize_text("".join(choice.itertext()))
                    cid = (choice.get("identifier") or "").strip()
                    if text:
                        options.append((cid, text))

            correct_id = response_map.get(response_id, "")
            correct_answer = ""
            for cid, text in options:
                if cid == correct_id:
                    correct_answer = text
                    break

            if not correct_answer:
                for choice in interaction.findall(f".//{qti}inlineChoice"):
                    feedback = choice.get("answerfeedback", "") or ""
                    if "#feedback:p1#" in feedback:
                        correct_answer = self._normalize_text(
                            "".join(choice.itertext())
                        )
                        correct_id = (choice.get("identifier") or "").strip()
                        break
                if not correct_answer:
                    for choice in interaction.findall(".//inlineChoice"):
                        feedback = choice.get("answerfeedback", "") or ""
                        if "#feedback:p1#" in feedback:
                            correct_answer = self._normalize_text(
                                "".join(choice.itertext())
                            )
                            correct_id = (choice.get("identifier") or "").strip()
                            break

            if not correct_answer and i <= len(fallback_answers):
                correct_answer = fallback_answers[i - 1]

            context = self._get_dropdown_context_clean(contentblock, qti, interaction)

            questions.append(
                {
                    "type": "Dropdown",
                    "question_number": i,
                    "question": context or instruction or f"Dropdown {i}",
                    "instruction": instruction,
                    "audio_filename": audio_filename,
                    "answers": [t for _, t in options],
                    "correct_answer": self._normalize_text(correct_answer),
                    "correct_id": correct_id,
                    "response_id": response_id,
                }
            )

        filled = sum(1 for q in questions if q["correct_answer"])
        print(
            f"  {Fore.CYAN}[DROPDOWN] parse ได้ {len(questions)} ข้อ, มีคำตอบ {filled} ข้อ{Style.RESET_ALL}"
        )
        return questions

    def _get_dropdown_context_clean(self, contentblock, qti, interaction):
        if contentblock is None:
            return ""
        try:
            for p in contentblock.findall(f".//{qti}p"):
                for sub in p.iter():
                    if sub is interaction:
                        parts = []
                        for child in p.iter():
                            if child is interaction:
                                parts.append("[...]")
                            if child.text:
                                parts.append(child.text)
                            if child.tail:
                                parts.append(child.tail)
                        text = "".join(parts)
                        text = re.sub(r"\s+", " ", text).strip()
                        return text[:300]

            for p in contentblock.findall(".//p"):
                for sub in p.iter():
                    if sub is interaction:
                        parts = []
                        for child in p.iter():
                            if child is interaction:
                                parts.append("[...]")
                            if child.text:
                                parts.append(child.text)
                            if child.tail:
                                parts.append(child.tail)
                        text = "".join(parts)
                        text = re.sub(r"\s+", " ", text).strip()
                        return text[:300]
        except Exception:
            pass
        return ""

    def _parse_text_entry(self, root, qti, interactions, instruction, audio_filename):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            response_id = (interaction.get("responseIdentifier") or "").strip()

            accepted_answers = []
            if response_id:
                for rd in root.findall(f".//{qti}responseDeclaration"):
                    if (rd.get("identifier") or "").strip() == response_id:
                        cr = rd.find(f".//{qti}correctResponse")
                        if cr is None:
                            cr = rd.find(".//correctResponse")
                        if cr is not None:
                            for v in cr.findall(f".//{qti}value"):
                                if v.text:
                                    accepted_answers.append(
                                        self._normalize_text(v.text)
                                    )
                            if not accepted_answers:
                                for v in cr.findall(".//value"):
                                    if v.text:
                                        accepted_answers.append(
                                            self._normalize_text(v.text)
                                        )
                        break

            contentblock = root.find(f'.//{qti}div[@id="contentblock"]')
            if contentblock is None:
                contentblock = root.find('.//div[@id="contentblock"]')
            context = self._get_dropdown_context_clean(contentblock, qti, interaction)

            questions.append(
                {
                    "type": "Text Entry",
                    "question_number": i,
                    "question": context or instruction or f"Gap {i}",
                    "instruction": instruction,
                    "audio_filename": audio_filename,
                    "answers": accepted_answers,
                    "correct_answer": accepted_answers[0] if accepted_answers else "",
                    "response_id": response_id,
                }
            )
        return questions

    def _parse_gap_match(self, root, qti, interactions, instruction, audio_filename):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            gap_texts = {}
            for gt in interaction.findall(f".//{qti}gapText"):
                gt_id = (gt.get("identifier") or "").strip()
                gt_text = self._normalize_text("".join(gt.itertext()))
                if gt_id and gt_text:
                    gap_texts[gt_id] = gt_text
            if not gap_texts:
                for gt in interaction.findall(".//gapText"):
                    gt_id = (gt.get("identifier") or "").strip()
                    gt_text = self._normalize_text("".join(gt.itertext()))
                    if gt_id and gt_text:
                        gap_texts[gt_id] = gt_text

            correct_pairs = []
            for rd in root.findall(f".//{qti}responseDeclaration"):
                cr = rd.find(f".//{qti}correctResponse")
                if cr is None:
                    cr = rd.find(".//correctResponse")
                if cr is not None:
                    for v in cr.findall(f".//{qti}value"):
                        if v.text:
                            correct_pairs.append(v.text.strip())
                    if not correct_pairs:
                        for v in cr.findall(".//value"):
                            if v.text:
                                correct_pairs.append(v.text.strip())

            id_to_text = {}
            for pair in correct_pairs:
                parts = pair.split()
                if len(parts) == 2:
                    gt_id, gap_id = parts
                    if gt_id in gap_texts:
                        id_to_text[gap_id] = gap_texts[gt_id]

            context_parts = []
            contentblock = root.find(f'.//{qti}div[@id="contentblock"]')
            if contentblock is None:
                contentblock = root.find('.//div[@id="contentblock"]')

            if contentblock is not None:
                for p in contentblock.findall(f".//{qti}p"):
                    text = "".join(p.itertext()).strip()
                    if (
                        not text
                        or text.startswith("#")
                        or text.startswith("Correct!")
                        or text.startswith("Incorrect!")
                    ):
                        continue
                    text = re.sub(r"\s+", " ", text)
                    context_parts.append(text)
                if not context_parts:
                    for p in contentblock.findall(".//p"):
                        text = "".join(p.itertext()).strip()
                        if (
                            not text
                            or text.startswith("#")
                            or text.startswith("Correct!")
                            or text.startswith("Incorrect!")
                        ):
                            continue
                        text = re.sub(r"\s+", " ", text)
                        context_parts.append(text)

            all_options = list(gap_texts.values())

            questions.append(
                {
                    "type": "Drag & Drop",
                    "question_number": i,
                    "question": " | ".join(context_parts)
                    if context_parts
                    else instruction,
                    "instruction": instruction,
                    "audio_filename": audio_filename,
                    "answers": all_options,
                    "correct_answer": "",
                    "correct_pairs": id_to_text,
                    "gap_texts": gap_texts,
                }
            )
        return questions

    async def get_answers_from_data_js(self):
        print(f"\n  {Fore.CYAN}[*] เริ่มดึงเฉลยจาก data.js...{Style.RESET_ALL}")

        if not self.data_js_urls:
            print(f"  {Fore.RED}[-] ไม่มี URL ของ data.js{Style.RESET_ALL}")
            return None

        all_answers = {}
        urls = list(dict.fromkeys(reversed(self.data_js_urls)))

        for idx, url in enumerate(urls, 1):
            content = None
            if url in self.data_js_contents:
                content = self.data_js_contents[url]
                print(
                    f"  {Fore.GREEN}[+] ใช้ content ที่ intercept ได้ ({len(content)} bytes){Style.RESET_ALL}"
                )
            else:
                try:
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()
                    content = response.text
                    print(
                        f"  {Fore.GREEN}[+] ดาวน์โหลดสำเร็จ ({len(content)} bytes){Style.RESET_ALL}"
                    )
                except Exception as e:
                    print(f"  {Fore.RED}[-] ดาวน์โหลดไม่สำเร็จ: {e}{Style.RESET_ALL}")
                    continue

            if not content:
                continue

            data = self._parse_js_object(content)
            if not data:
                continue

            xml_count = 0
            total_questions = 0
            for filename in sorted(data.keys()):
                if not filename.endswith(".xml"):
                    continue

                if (
                    "learningobjectinfo" in filename.lower()
                    or "manifest" in filename.lower()
                ):
                    continue
                xml_count += 1
                xml_content = data[filename]

                if "You have finished the activity." in str(xml_content):
                    continue

                decoded_xml = self._decode_unicode_escapes(xml_content)
                questions = self._extract_answers_from_xml(decoded_xml)
                if questions:
                    all_answers[filename] = questions
                    total_questions += len(questions)
                    print(
                        f"      {Fore.GREEN}[+] {filename}: ได้ {len(questions)} ข้อ{Style.RESET_ALL}"
                    )

            print(
                f"  {Fore.CYAN}[PARSE] รวม {xml_count} XML, ได้ {total_questions} คำถาม{Style.RESET_ALL}"
            )

            if all_answers:
                break

        if not all_answers:
            print(f"\n  {Fore.RED}[-] ไม่พบเฉลยจาก data.js{Style.RESET_ALL}")
            return None

        return all_answers

    def _print_exercise_answers_by_file(self, exercise_name, answers_by_file):

        print(f"\n{Fore.CYAN}{exercise_name}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

        if not answers_by_file:
            print(f"  {Fore.YELLOW}[!] ไม่พบเฉลย{Style.RESET_ALL}")
            return

        question_number = 0
        for filename, questions in answers_by_file.items():
            print(f"\n  {Fore.MAGENTA}[{filename}]{Style.RESET_ALL}")
            for question in questions:
                question_number += 1
                question_type = question.get("type", "Unknown")
                prompt = question.get("question") or question.get("instruction") or "-"
                print(f"\n      [{question_number}] {question_type}")
                print(f"      คำถาม: {prompt}")

                if question_type == "Drag & Drop":
                    pairs = question.get("correct_pairs", {})
                    if pairs:
                        print("      คำตอบ:")
                        for gap_id, answer in pairs.items():
                            print(f"        {gap_id}: {answer}")
                    continue

                correct_answers = question.get("correct_answers") or []
                correct_answer = question.get("correct_answer", "")
                if correct_answers:
                    answer_text = ", ".join(correct_answers)
                else:
                    answer_text = correct_answer or "-"
                print(f"      คำตอบ: {Fore.GREEN}{answer_text}{Style.RESET_ALL}")

        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

    async def _get_answers_from_page(
        self, exercise_name, go_back_after=False, already_open=False
    ):
        if not already_open:
            if not await self.click_exercise(exercise_name):
                return None

        await self.safe_wait_for_load()
        await self.debug_wait(0.5)

        answers = await self.page.evaluate(
            """
            () => {
                const result = {
                    question: '',
                    options: [],
                    correct_answers: []
                };

                const questionElem = document.querySelector('.question-text, .question');
                if (questionElem) {
                    result.question = questionElem.innerText.trim();
                }

                const optionElements = document.querySelectorAll('.option, .choice, [class*="option"]');

                optionElements.forEach(el => {
                    const text = (el.innerText || '').trim();
                    if (!text) return;

                    const selected = el.querySelector('input:checked, input[aria-checked="true"], .selected');

                    result.options.push({
                        text: text,
                        is_correct: !!selected
                    });

                    if (selected) {
                        result.correct_answers.push(text);
                    }
                });

                return result;
            }
        """
        )

        print(f"\n  {Fore.CYAN}{exercise_name}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        if answers.get("question"):
            print(f"\n      คําถาม: {answers['question']}\n")

        if answers.get("options"):
            print(f"      ตัวเลือก:")
            for i, option in enumerate(answers["options"], 1):
                if option.get("is_correct"):
                    print(
                        f"        {Fore.GREEN}{i}. {option['text']}   <-- คําตอบ{Style.RESET_ALL}"
                    )
                else:
                    print(f"        {Fore.WHITE}{i}. {option['text']}{Style.RESET_ALL}")

        if answers.get("correct_answers"):
            print(
                f"      {Fore.GREEN}คําตอบ: {', '.join(answers['correct_answers'])}{Style.RESET_ALL}"
            )

        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        if go_back_after:
            try:
                input(
                    f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับไปเลือกรายการ...{Style.RESET_ALL}"
                )
            except Exception:
                pass
            await self.page.go_back()
            await self.safe_wait_for_load()

        return answers

    async def close(self):
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass


def display_courses(courses):
    print(f"\n{Fore.CYAN}คอร์สที่มี Digital Workbook{Style.RESET_ALL}\n")
    if not courses:
        print(f"  {Fore.YELLOW}[!] ไม่พบคอร์ส{Style.RESET_ALL}")
        return
    for i, course in enumerate(courses, 1):
        print(
            f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {Fore.CYAN}{course['courseName']}{Style.RESET_ALL}"
        )
        print(f"      {Fore.GREEN}{course['workbookName']}{Style.RESET_ALL}\n")


def display_units(units):
    print(f"\n{Fore.CYAN}เลือกบทที่ต้องการดู{Style.RESET_ALL}\n")
    if not units:
        print(f"  {Fore.YELLOW}[!] ไม่พบบทเรียน{Style.RESET_ALL}")
        return
    for i, unit in enumerate(units, 1):
        print(
            f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {Fore.CYAN}{unit['name']}{Style.RESET_ALL}"
        )


def display_exercises(data):
    print(f"\n{Fore.CYAN}{data['unit_name']}{Style.RESET_ALL}\n")
    if not data.get("lessons"):
        print(f"  {Fore.YELLOW}[!] ไม่มีแบบฝึกหัด{Style.RESET_ALL}")
        return
    exercise_counter = 1
    for lesson in data["lessons"]:
        print(f"  {Fore.MAGENTA}{lesson['name']}{Style.RESET_ALL}")
        for ex in lesson["exercises"]:
            if ex["status"] == "completed":
                print(
                    f"  {Fore.GREEN}[{exercise_counter}] [OK] {ex['name']}{Style.RESET_ALL}"
                )
            elif ex["status"] == "in-progress":
                print(
                    f"  {Fore.YELLOW}[{exercise_counter}] [..] {ex['name']}{Style.RESET_ALL}"
                )
            else:
                print(
                    f"  {Fore.WHITE}[{exercise_counter}] [  ] {ex['name']}{Style.RESET_ALL}"
                )
            exercise_counter += 1
        print()


def choose_account(scraper):
    accounts = scraper.load_accounts()
    print(f"\n{Fore.CYAN}บัญชี Cambridge One / Chrome profiles{Style.RESET_ALL}\n")

    if accounts:
        for i, account in enumerate(accounts, 1):
            has_pwd = " [saved password]" if account.get("password") else ""
            print(f"  [{i}] {account.get('email', '')}{has_pwd}")
        print("  [N] เพิ่มบัญชีใหม่")
        print("  [D] ลบรหัสผ่านที่บันทึกไว้")
        print("  [0] ออกจากโปรแกรม")
        try:
            choice = input(f"\n{Fore.CYAN}[>] เลือกบัญชี: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "1"

        if choice == "0":
            return None, None, False
        if choice.lower() == "n":
            try:
                email = input(
                    f"\n  {Fore.CYAN}[>] Email บัญชีใหม่: {Style.RESET_ALL}"
                ).strip()
            except Exception:
                return None, None, False
            if not email:
                return None, None, False
            return email, None, True
        if choice.lower() == "d":
            try:
                email = input(
                    f"\n  {Fore.CYAN}[>] Email ที่ต้องการลบรหัสผ่าน: {Style.RESET_ALL}"
                ).strip()
            except Exception:
                email = ""
            if email:
                scraper.delete_account_password(email)
                print(f"  {Fore.GREEN}[+] ลบรหัสผ่านแล้ว{Style.RESET_ALL}")
            return None, None, False
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(accounts):
                acc = accounts[idx]
                return acc.get("email", ""), acc.get("password"), True
        except ValueError:
            pass
        print(f"  {Fore.RED}[-] ตัวเลือกไม่ถูกต้อง{Style.RESET_ALL}")
        return None, None, False

    try:
        email = input(f"\n  {Fore.CYAN}[>] Email: {Style.RESET_ALL}").strip()
    except Exception:
        return None, None, False
    if not email:
        return None, None, False
    return email, None, True


async def auto_process_single_unit(
    scraper,
    selected_course,
    selected_unit,
    exercises_data,
    do_all=False,
    answer_source=None,
):
    all_exercises = []
    for lesson in exercises_data.get("lessons", []):
        for ex in lesson["exercises"]:
            all_exercises.append(
                {"lesson": lesson["name"], "name": ex["name"], "status": ex["status"]}
            )

    if do_all:
        exercises_to_do = all_exercises
        print(
            f"  {Fore.CYAN}[*] โหมดทำทุกข้อ: {len(exercises_to_do)} ข้อ{Style.RESET_ALL}"
        )
    else:
        exercises_to_do = [ex for ex in all_exercises if ex["status"] != "completed"]
        print(
            f"  {Fore.CYAN}[*] โหมดทำเฉพาะข้อที่ยังไม่ทำ: {len(exercises_to_do)} ข้อ{Style.RESET_ALL}"
        )

    if not exercises_to_do:
        print(f"  {Fore.GREEN}[+] ไม่มีข้อที่ต้องทำในบทนี้!{Style.RESET_ALL}")
        return [], []

    if answer_source is None:
        answer_source = scraper._choose_answer_source()
    scraper.answer_source = answer_source
    print(f"  {Fore.CYAN}[*] ระบบหาคำตอบ: {answer_source}{Style.RESET_ALL}")

    total_count = len(exercises_to_do)
    summary = []
    skipped = []

    for i, ex in enumerate(exercises_to_do, 1):
        print(
            f"\n{Fore.CYAN}[{i}/{total_count}] กำลังทำ: {ex['name']}{Style.RESET_ALL}"
        )
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

        scraper.reset_data_js_tracking()

        if not await scraper.click_exercise(ex["name"]):
            summary.append(
                {
                    "name": ex["name"],
                    "status": "FAILED",
                    "reason": "เปิดแบบฝึกหัดไม่ได้",
                }
            )
            continue

        try:
            is_video_tf = await scraper.check_video_true_false()
        except Exception:
            is_video_tf = False

        if is_video_tf:
            has_data = await scraper._wait_for_data_js(timeout=10)
            if not has_data:
                skipped.append({"name": ex["name"], "reason": "ไม่พบ data.js"})
                try:
                    await scraper.page.go_back()
                    await scraper.safe_wait_for_load()
                except Exception:
                    pass
                continue

        source_for_this = answer_source
        if answer_source == "ask":
            try:
                src = (
                    input(
                        f"\n  {Fore.CYAN}[>] ข้อ '{ex['name']}' - ใช้คำตอบจาก DB หรือดึงจาก data.js? (db/extract/skip): {Style.RESET_ALL}"
                    )
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                src = "extract"
            if src in ("db", "database"):
                source_for_this = "db"
            elif src in ("skip", "s"):
                source_for_this = "skip"
            else:
                source_for_this = "extract"

        if source_for_this == "db":
            cached = scraper.db_get(
                selected_course["courseName"], selected_unit["name"], ex["name"]
            )
            if cached:
                print(
                    f"  {Fore.CYAN}[*] ใช้คำตอบจากฐานข้อมูลที่มีอยู่แล้ว{Style.RESET_ALL}"
                )
                answers = cached
            else:
                print(
                    f"  {Fore.YELLOW}[!] ไม่พบคำตอบในฐานข้อมูลสำหรับข้อนี้ — ข้ามข้อนี้{Style.RESET_ALL}"
                )
                answers = None
        elif source_for_this == "skip":
            print(
                f"  {Fore.YELLOW}[!] ข้ามข้อ '{ex['name']}' ตามที่ผู้ใช้เลือก{Style.RESET_ALL}"
            )
            try:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
                await scraper.debug_wait(1)
            except Exception:
                pass
            skipped.append({"name": ex["name"], "reason": "ผู้ใช้เลือกข้าม"})
            continue
        else:

            await scraper._wait_for_data_js(timeout=20)
            answers = await scraper.get_answers_from_data_js()
            if answers:
                scraper.db_set(
                    selected_course["courseName"],
                    selected_unit["name"],
                    ex["name"],
                    answers,
                )
                print(f"  {Fore.CYAN}[*] บันทึกคำตอบลงฐานข้อมูลแล้ว{Style.RESET_ALL}")
            else:
                answers = None

        fill_result = await scraper.auto_fill_and_check(ex["name"], answers)

        if not fill_result["success"]:
            summary.append(
                {
                    "name": ex["name"],
                    "status": "FAILED",
                    "reason": fill_result["reason"],
                    "score": fill_result.get("score"),
                }
            )
            print(
                f"  {Fore.RED}[!] ข้อ '{ex['name']}' ทำไม่สำเร็จ: {fill_result['reason']}{Style.RESET_ALL}"
            )
            try:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
                await scraper.debug_wait(1)
            except Exception:
                pass
            continue

        await scraper.check_and_retry_if_needed(ex["name"], answers)

        is_multi_page = await scraper.is_multi_question_mc()
        if not is_multi_page:
            await scraper.submit_single_page_activity()

        final_score, total_score = await scraper._get_score_from_page()
        if final_score is not None:
            score_str = f"{final_score}/{total_score}"
            is_full = final_score == total_score
            if is_full:
                print(
                    f"  {Fore.GREEN}[] คะแนน: {score_str} — เต็มคะแนน ไม่มีปัญหาในการทำแบบฝึกหัด{Style.RESET_ALL}"
                )
            else:
                print(
                    f"  {Fore.RED}[] คะแนน: {score_str} — ไม่เต็มคะแนน โปรแกรมอาจมีปัญหาในการทำแบบฝึกหัดข้อนี้{Style.RESET_ALL}"
                )
        else:
            score_str = "ไม่ทราบคะแนน"
            print(
                f"  {Fore.YELLOW}[!] คะแนน: {score_str} — ไม่สามารถอ่านคะแนนจากหน้าเว็บได้{Style.RESET_ALL}"
            )

        summary.append(
            {
                "name": ex["name"],
                "status": "OK",
                "score": score_str,
                "full_score": is_full if final_score is not None else None,
            }
        )

        await scraper.debug_wait(2)

    return summary, skipped


async def run_all_units_auto(scraper, selected_course, do_all=False):
    print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN} โหมดทำทุกบท ทุกงาน{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

    units = await scraper.get_all_units()
    if not units:
        return

    print(f"  {Fore.GREEN}[+] พบ {len(units)} บทเรียน{Style.RESET_ALL}")

    all_summary = []
    all_skipped = []

    for unit_idx, unit in enumerate(units, 1):
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")
        print(
            f"  {Fore.CYAN}[Unit {unit_idx}/{len(units)}] กำลังทำบท: {unit['name']}{Style.RESET_ALL}"
        )
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

        try:
            current_url = scraper.page.url
            if "/view/" in current_url or "/activity/" in current_url:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
                await scraper.debug_wait(2)
        except Exception:
            pass

        if not await scraper.click_unit(unit["name"]):
            continue

        exercises_data = await scraper.get_exercises_in_unit(unit["name"])
        summary, skipped = await auto_process_single_unit(
            scraper, selected_course, unit, exercises_data, do_all=do_all
        )
        all_summary.extend(summary)
        all_skipped.extend(skipped)

        try:
            await scraper.page.go_back()
            await scraper.safe_wait_for_load()
            await scraper.debug_wait(2)
        except Exception:
            pass

    print(f"\n  {Fore.CYAN} สรุปผลการทำงานทั้งหมด{Style.RESET_ALL}")

    ok_count = sum(1 for s in all_summary if s["status"] == "OK")
    fail_count = sum(1 for s in all_summary if s["status"] == "FAILED")

    for i, s in enumerate(all_summary, 1):
        if s["status"] == "OK":
            score_str = f" (Score: {s['score']})" if s.get("score") else ""
            problem = ""
            if s.get("score") and s.get("full_score") is False:
                problem = f" {Fore.RED}[ไม่เต็มคะแนน — อาจมีปัญหา]{Style.RESET_ALL}"
            print(
                f"  [{i}] {Fore.GREEN} {s['name']}{score_str}{problem}{Style.RESET_ALL}"
            )
        else:
            print(
                f"  [{i}] {Fore.RED} {s['name']} — {s.get('reason', '')}{Style.RESET_ALL}"
            )

    print(f"\n  {Fore.GREEN}สำเร็จ: {ok_count}{Style.RESET_ALL}")
    if fail_count > 0:
        print(f"  {Fore.RED}ล้มเหลว: {fail_count}{Style.RESET_ALL}")


async def run_unit_menu(scraper, selected_course, selected_unit, exercises_data):
    while True:
        display_exercises(exercises_data)

        print(f"\n  {Fore.CYAN}เลือกสิ่งที่ต้องการทำ:{Style.RESET_ALL}")
        print(f"  [1] ทำแบบฝึกหัดอัตโนมัติทั้งหมดในบทนี้")
        print(f"  [0] กลับไปเลือกบทใหม่")

        try:
            action_choice = (
                input(f"\n  {Fore.CYAN}[>] เลือก (0-1): {Style.RESET_ALL}")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            action_choice = "0"

        if action_choice == "1":
            summary, skipped = await auto_process_single_unit(
                scraper, selected_course, selected_unit, exercises_data, do_all=True
            )
            ok_count = sum(1 for s in summary if s["status"] == "OK")
            for i, s in enumerate(summary, 1):
                if s["status"] == "OK":
                    score_str = f" (Score: {s['score']})" if s.get("score") else ""
                    problem = ""
                    if s.get("score") and s.get("full_score") is False:
                        problem = (
                            f" {Fore.RED}[ไม่เต็มคะแนน — อาจมีปัญหา]{Style.RESET_ALL}"
                        )
                    print(
                        f"  [{i}] {Fore.GREEN} {s['name']}{score_str}{problem}{Style.RESET_ALL}"
                    )
                else:
                    print(
                        f"  [{i}] {Fore.RED} {s['name']} — {s.get('reason', '')}{Style.RESET_ALL}"
                    )
            print(f"\n  {Fore.GREEN}สำเร็จ: {ok_count}{Style.RESET_ALL}")

            try:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
            except Exception:
                pass

            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass

        elif action_choice == "0":
            return

        else:
            print(f"  {Fore.RED}[-] Invalid option{Style.RESET_ALL}")
            await scraper.debug_wait(1)


async def main_async():
    print(
        f"""
    {Fore.GREEN}
     a88888b.  888888ba   .88888.     d888888P                   dP 
    d8'   `88  88    `8b d8'   `8b       88                      88 
    88        a88aaaa8P' 88     88       88    .d8888b. .d8888b. 88 
    88         88   `8b. 88     88       88    88'  `88 88'  `88 88 
    Y8.   .88  88    .88 Y8.   .8P       88    88.  .88 88.  .88 88 
     Y88888P'  88888888P  `8888P'        dP    `88888P' `88888P' dP 
                                                                                                                                                                                                                                                                         
    {Style.RESET_ALL}
    by z3nTr4ry"""
    )
    print(
        f"    {Fore.GREEN}[+] เบราว์เซอร์จะไม่ปิดจนกว่าจะจบการทำงาน{Style.RESET_ALL}\n"
    )

    scraper = CambridgeOneScraper()
    email, saved_password, should_continue = choose_account(scraper)
    if not should_continue or not email:
        print(f"\n  {Fore.YELLOW}[!] Exiting...{Style.RESET_ALL}")
        return

    try:
        profile_dir = scraper._profile_dir(email)
        print(
            f"  {Fore.CYAN}[*] กำลังเปิด Chrome data profile: {profile_dir}{Style.RESET_ALL}"
        )
        await scraper.init_browser(profile_dir)
        print(f"  {Fore.GREEN}[+] เปิดเบราว์เซอร์แบบเก็บ session แล้ว{Style.RESET_ALL}")

        login_success = await scraper.check_login_status()

        if login_success:
            print(f"  {Fore.GREEN}[+] ใช้ session เดิมได้เลย{Style.RESET_ALL}")
        else:
            password = None
            if saved_password:
                print(f"  {Fore.GREEN}[+] ใช้รหัสผ่านที่บันทึกไว้{Style.RESET_ALL}")
                password = saved_password
            else:
                password = getpass.getpass(
                    f"  {Fore.CYAN}[>] Password ของ {email}: {Style.RESET_ALL}"
                ).strip()
                if password:
                    try:
                        save_choice = (
                            input(
                                f"  {Fore.CYAN}[>] บันทึกรหัสผ่าน? (y/n): {Style.RESET_ALL}"
                            )
                            .strip()
                            .lower()
                        )
                    except Exception:
                        save_choice = "y"

                    if save_choice == "y":
                        scraper.save_account(email, password)
                        print(f"  {Fore.GREEN}[+] บันทึกรหัสผ่านแล้ว{Style.RESET_ALL}")

            if not password:
                print(f"  {Fore.RED}[-] ไม่ได้ใส่รหัสผ่าน{Style.RESET_ALL}")
                await scraper.close()
                return

            login_success = await scraper.login(email, password)
            if login_success:
                scraper.save_account(email, password)

        if not login_success:
            print(f"  {Fore.RED}[-] Login ไม่สำเร็จ{Style.RESET_ALL}")
            await scraper.close()
            return

        await scraper.debug_wait(1)

        while True:
            courses = await scraper.get_courses_with_workbooks()
            display_courses(courses)
            if not courses:
                print(f"  {Fore.YELLOW}[!] ไม่พบคอร์ส{Style.RESET_ALL}")
                try:
                    input(
                        f"\n  {Fore.CYAN}[>] Press Enter to close browser...{Style.RESET_ALL}"
                    )
                except Exception:
                    pass
                await scraper.close()
                return

            print(f"  {Fore.CYAN}เลือกคอร์สที่ต้องการเปิด:{Style.RESET_ALL}")
            for i, course in enumerate(courses, 1):
                print(f"  {i}. {course['courseName']}")
            print(f"  0. รีเฟรชรายการคอร์ส")
            print(f"  q. ออกจากโปรแกรม")

            try:
                choice = input(
                    f"\n  {Fore.CYAN}[>] Enter class number: {Style.RESET_ALL}"
                ).strip()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            if choice.lower() == "q":
                break
            if choice == "0":
                continue

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(courses):
                    selected_course = courses[idx]
                    success = await scraper.click_workbook(
                        selected_course["courseName"]
                    )
                    if not success:
                        continue

                    await run_unit_loop(scraper, selected_course)
                else:
                    print(f"  {Fore.RED}[-] Invalid class number{Style.RESET_ALL}")
            except ValueError:
                print(f"  {Fore.RED}[-] Please enter a valid number{Style.RESET_ALL}")

        try:
            input(
                f"\n  {Fore.CYAN}[>] Press Enter to close browser...{Style.RESET_ALL}"
            )
        except Exception:
            pass

    except Exception as e:
        print(f"\n  {Fore.RED}[-] Error: {e}{Style.RESET_ALL}")
        traceback.print_exc()

    finally:
        print(f"  {Fore.CYAN}[*] กำลังปิดเบราว์เซอร์...{Style.RESET_ALL}")
        await scraper.close()
        print(f"  {Fore.GREEN}[+] ปิดเบราว์เซอร์เรียบร้อย{Style.RESET_ALL}")


async def run_unit_loop(scraper, selected_course):
    while True:
        units = await scraper.get_all_units()
        if not units:
            print(f"  {Fore.YELLOW}[!] ไม่พบบทเรียน{Style.RESET_ALL}")
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass
            return

        display_units(units)
        print(f"\n  {Fore.CYAN}[0] กลับไปเลือกคอร์สใหม่{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[r] Refresh รายการบท{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[q] ออกจากโปรแกรม{Style.RESET_ALL}")

        try:
            unit_choice = input(
                f"\n  {Fore.CYAN}[>] เลือกบท (ตัวเลข): {Style.RESET_ALL}"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            unit_choice = "0"

        if unit_choice.lower() == "q":
            sys.exit(0)
        if unit_choice == "0":
            return
        if unit_choice.lower() == "r":
            continue

        try:
            unit_idx = int(unit_choice) - 1
            if 0 <= unit_idx < len(units):
                selected_unit = units[unit_idx]
                await scraper.click_unit(selected_unit["name"])
                exercises_data = await scraper.get_exercises_in_unit(
                    selected_unit["name"]
                )
                await run_unit_menu(
                    scraper, selected_course, selected_unit, exercises_data
                )
            else:
                print(f"  {Fore.RED}[-] Invalid unit number{Style.RESET_ALL}")
                try:
                    input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
                except Exception:
                    pass
        except ValueError:
            print(f"  {Fore.RED}[-] Please enter a valid number{Style.RESET_ALL}")
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
            except Exception:
                pass


def main():
    logger = TeeLogger(log_dir="logs")
    sys.stdout = logger
    sys.stderr = logger

    try:
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}Session log: {logger.log_file}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'-' * 50}{Style.RESET_ALL}")

        asyncio.run(main_async())

    except Exception as e:
        print(f"\n  {Fore.RED}[-] Fatal Error: {e}{Style.RESET_ALL}")
        traceback.print_exc()
    finally:
        print(
            f"\n  {Fore.CYAN}[*] บันทึก log ไปที่: {logger.log_file}{Style.RESET_ALL}"
        )
        logger.close()
        sys.stdout = logger.original_stdout
        sys.stderr = logger.original_stderr


if __name__ == "__main__":
    main()
