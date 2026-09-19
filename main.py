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

try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

init(autoreset=True)


def clear_screen():
    pass


# ==================== Tee Logger ====================
class TeeLogger:
    def __init__(self, log_dir="logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"session_{ts}.log"
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        try:
            self.file = open(self.log_file, 'w', encoding='utf-8')
        except Exception:
            self.file = None
            self.original_stdout.write(
                f"[!] ไม่สามารถเปิด log file: {self.log_file}\n"
            )

    def write(self, message):
        try:
            self.original_stdout.write(message)
        except Exception:
            pass
        if self.file:
            try:
                clean = re.sub(r'\x1b\[[0-9;]*m', '', message)
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


# ==================== Cambridge One Scraper ====================
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
        self.last_data_js_url = None
        self.log_file_path = None
        self.tts_engine = None
        self.start_time = 0
        self.exercise_times = {}

        if HAS_TTS:
            try:
                self.tts_engine = pyttsx3.init()
                self.tts_engine.setProperty('rate', 140)
            except Exception:
                self.tts_engine = None

    def reset_data_js_tracking(self):
        self.data_js_urls.clear()
        self.data_js_contents.clear()
        print(f"  {Fore.CYAN}[DATA.JS] เคลียร์ข้อมูลเก่าเรียบร้อย{Style.RESET_ALL}")

    def _load_answers_db(self):
        try:
            if self.answers_db_file.exists():
                with open(self.answers_db_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_answers_db(self):
        try:
            with open(self.answers_db_file, 'w', encoding='utf-8') as f:
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

    def _account_key(self, email):
        return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:20]

    def _profile_dir(self, email):
        return self.profiles_dir / self._account_key(email)

    def load_accounts(self):
        try:
            if self.accounts_file.exists():
                with open(self.accounts_file, 'r', encoding='utf-8') as f:
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
        existing = next((a for a in accounts if a.get('key') == key), None)
        entry = {'key': key, 'email': email}
        if password:
            entry['password'] = password
        elif existing and existing.get('password'):
            entry['password'] = existing['password']
        accounts = [a for a in accounts if a.get('key') != key]
        accounts.insert(0, entry)
        try:
            with open(self.accounts_file, 'w', encoding='utf-8') as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def get_saved_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get('key') == key and a.get('password'):
                return a.get('password')
        return None

    def delete_account_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get('key') == key:
                a.pop('password', None)
        try:
            with open(self.accounts_file, 'w', encoding='utf-8') as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    async def debug_wait(self, seconds=0.5):
        await asyncio.sleep(seconds)

    async def safe_wait_for_load(self, timeout=8000):
        try:
            await self.page.wait_for_load_state('domcontentloaded', timeout=timeout)
            await self.page.wait_for_load_state('networkidle', timeout=timeout)
        except Exception:
            pass

    async def wait_for_loading_spinner(self, timeout=15):
        try:
            await self.page.wait_for_selector('.loading, .spinner, [class*="spinner"], [class*="loading"]', state='detached', timeout=timeout*1000)
        except Exception:
            pass

    async def check_video_true_false(self):
        try:
            for frame in self.page.frames:
                has_choice_interaction = await frame.evaluate('''
                    () => {
                        return document.querySelectorAll('.choice_interaction').length > 0;
                    }
                ''')
                if has_choice_interaction:
                    return True
            return False
        except Exception:
            return False

    # ==================== Iframe Helper ====================

    async def _get_activity_frame(self, timeout=5, force_refresh=False):
        """หา Iframe ที่มี activity"""
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            for frame in self.page.frames:
                try:
                    count = await frame.evaluate('''
                        () => document.querySelectorAll('.choice_interaction, .wrapper-dropdown, .input-radio, input[type="text"], .drag_element, .drop_area').length
                    ''')
                    if count > 0:
                        return frame
                except Exception:
                    continue
            await asyncio.sleep(0.3)
        return None

    # ==================== Interactive Speaking ====================

    async def _find_skip_button(self):
        selectors = [
            'button:has-text("Skip")',
            'a:has-text("Skip")',
            '.skip-btn',
            '[class*="skip"]',
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

    async def _find_next_activity_button(self):
        selectors = [
            'a.nextActivityBtn',
            'a:has-text("NEXT ACTIVITY")',
            'button:has-text("NEXT ACTIVITY")',
            'a:has-text("Next activity")',
            'button:has-text("Next activity")',
            '.nextActivityBtn.btn',
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

    async def _detect_interactive_speaking(self):
        try:
            for frame in self.page.frames:
                has_skip = await frame.evaluate('''
                    () => {
                        const btns = document.querySelectorAll('button, a');
                        for (const b of btns) {
                            const txt = (b.innerText || '').trim().toLowerCase();
                            if (txt === 'skip' && b.offsetHeight > 0) {
                                return true;
                            }
                        }
                        return false;
                    }
                ''')
                if has_skip:
                    print(f"  {Fore.CYAN}[*] ตรวจพบปุ่ม Skip — เป็น Interactive Speaking{Style.RESET_ALL}")
                    return frame
            return None
        except Exception:
            return None

    async def handle_interactive_speaking(self):
        print(f"\n  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[*] ตรวจพบ Interactive Speaking — ใช้วิธี Skip + Next{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        skip_count = 0
        max_skip = 15

        for round_num in range(1, max_skip + 1):
            print(f"\n  {Fore.CYAN}[*] === รอบที่ {round_num}/{max_skip} ==={Style.RESET_ALL}")

            skip_btn = await self._find_skip_button()
            if skip_btn:
                print(f"  {Fore.YELLOW}[+] พบปุ่ม Skip — กดข้าม...{Style.RESET_ALL}")
                try:
                    await skip_btn.scroll_into_view_if_needed()
                    await skip_btn.click(force=True)
                except Exception as e:
                    print(f"  {Fore.RED}[!] กด Skip ไม่ได้: {e}{Style.RESET_ALL}")
                skip_count += 1
                await self.debug_wait(3)
                continue

            next_btn = await self._find_next_activity_button()
            if next_btn:
                print(f"  {Fore.GREEN}[+] พบปุ่ม NEXT ACTIVITY — กดผ่าน...{Style.RESET_ALL}")
                try:
                    await next_btn.scroll_into_view_if_needed()
                    await next_btn.click(force=True)
                except Exception:
                    pass
                await self.debug_wait(3)
                await self.safe_wait_for_load()
                return True

            try:
                body_text = await self.page.inner_text('body')
                if 'NEXT ACTIVITY' in body_text or 'Next activity' in body_text:
                    print(f"  {Fore.GREEN}[+] พบข้อความ NEXT ACTIVITY — จบแล้ว{Style.RESET_ALL}")
                    return True
            except Exception:
                pass

            if skip_count > 5:
                print(f"  {Fore.YELLOW}[!] กด Skip ไปหลายรอบแล้วยังไม่เจอ Next — ลองหาปุ่ม Speak{Style.RESET_ALL}")
                speak_btn = None
                for frame in self.page.frames:
                    try:
                        btn = await frame.query_selector('button:has-text("Speak"), button:has-text("Try again")')
                        if btn and await btn.is_visible():
                            speak_btn = btn
                            break
                    except Exception:
                        continue
                
                if speak_btn:
                    print(f"  {Fore.CYAN}[*] พบปุ่ม Speak — กดเพื่อไปต่อ{Style.RESET_ALL}")
                    try:
                        await speak_btn.click(force=True)
                        await self.debug_wait(3)
                        continue
                    except Exception:
                        pass

            print(f"  {Fore.CYAN}[*] รอ 2 วินาที แล้วตรวจสอบใหม่...{Style.RESET_ALL}")
            await self.debug_wait(2)

        print(f"  {Fore.YELLOW}[!] ครบ {max_skip} รอบแล้ว — ถือว่าจบ{Style.RESET_ALL}")
        return True

    # ==================== auto_fill_and_check ====================

    async def auto_fill_and_check(self, exercise_name, answers_by_file):
        self.start_time = time.time()
        print(f"\n  {Fore.CYAN}[*] กำลังเติมคำตอบอัตโนมัติ: {exercise_name}{Style.RESET_ALL}")

        result = {
            'success': False,
            'reason': '',
            'filled': 0,
            'total': 0,
            'failed': [],
            'score': None
        }

        # 1. ตรวจสอบ Interactive Speaking ก่อน
        speaking_frame = await self._detect_interactive_speaking()
        if speaking_frame:
            print(f"  {Fore.CYAN}[*] ตรวจพบ Interactive Speaking — ใช้ระบบ Skip + Next{Style.RESET_ALL}")
            success = await self.handle_interactive_speaking()
            if success:
                result['success'] = True
                result['reason'] = 'OK (Interactive Speaking — Skipped)'
                return result
            else:
                result['reason'] = 'Interactive Speaking ทำไม่สำเร็จ'
                return result

        # 2. ตรวจสอบ Speaking Activity แบบเดิม
        is_speaking = await self.handle_speaking_activity()
        if is_speaking:
            await self.debug_wait(2)
            success = await self._click_check_button()
            if success:
                result['success'] = True
                result['reason'] = 'OK (Speaking Activity)'
                return result
            else:
                result['reason'] = 'อัดเสียงสำเร็จแต่ไม่พบปุ่ม Check'
                return result

        all_questions = []
        if answers_by_file:
            for _, questions in answers_by_file.items():
                all_questions.extend(questions)

        if not all_questions:
            result['reason'] = 'ไม่มีเฉลยให้เติม'
            print(f"  {Fore.YELLOW}[!] {result['reason']}{Style.RESET_ALL}")
            return result

        # Debug: แสดงประเภทของคำถามที่พบ
        type_counts = {}
        for q in all_questions:
            t = q.get('type', 'Unknown')
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"  {Fore.CYAN}[*] ประเภทคำถามที่พบ: {type_counts}{Style.RESET_ALL}")

        mc_questions = [q for q in all_questions if q.get('type') == 'Multiple Choice']
        is_multi_page = await self.is_multi_question_mc()

        if is_multi_page and len(mc_questions) > 0:
            print(f"  {Fore.CYAN}[*] ตรวจพบ Multi-Page MC — เริ่มทำทีละหน้าจนจบ{Style.RESET_ALL}")
            success = await self.continue_multi_question(exercise_name, answers_by_file)
            if success:
                result['success'] = True
                result['reason'] = 'OK (Multi-Page)'
                result['filled'] = 1
                result['total'] = 1
            else:
                result['reason'] = 'Multi-Page ทำไม่สำเร็จ'
            return result

        # โหมดปกติ (Single-Page)
        print(f"  {Fore.CYAN}[*] โหมด Single-Page{Style.RESET_ALL}")

        # 1. Click-to-fill (Drag & Drop)
        dd_questions = [q for q in all_questions if q.get('type') == 'Drag & Drop']
        if dd_questions:
            print(f"  {Fore.CYAN}[*] จะเติม Drag & Drop {len(dd_questions)} ข้อ...{Style.RESET_ALL}")
            for q in dd_questions:
                filled = await self._fill_click_to_fill(q)
                if filled:
                    result['filled'] += 1
                else:
                    result['failed'].append(q.get('question_number', 'Drag & Drop'))
            result['total'] += len(dd_questions)
            if result['filled'] < len(dd_questions):
                print(f"  {Fore.YELLOW}[!] เติม Drag & Drop ได้ {result['filled']}/{len(dd_questions)} ช่อง — ลองกด Check ต่อ{Style.RESET_ALL}")
        await self.debug_wait(1)

        # 2. Text Entry
        te_questions = [q for q in all_questions if q.get('type') == 'Text Entry']
        for q in te_questions:
            await self._fill_text_entry(q)
        await self.debug_wait(1)

        # 3. Multiple Choice
        mc_questions = [q for q in all_questions if q.get('type') == 'Multiple Choice']
        radio_questions = [q for q in mc_questions if not q.get('is_multiple_answers')]
        checkbox_questions = [q for q in mc_questions if q.get('is_multiple_answers')]

        if radio_questions:
            print(f"  {Fore.CYAN}[*] จะเติม Multiple Choice (Radio) {len(radio_questions)} ข้อ...{Style.RESET_ALL}")
            for q in radio_questions:
                q_idx = q.get('question_number', 1) - 1
                ok = await self._fill_multiple_choice_by_index(q, q_idx)
                if ok:
                    result['filled'] += 1
                else:
                    result['failed'].append(q.get('response_id', f'MC#{q.get("question_number")}'))
            result['total'] += len(radio_questions)

        if checkbox_questions:
            print(f"  {Fore.CYAN}[*] จะเติม Multiple Choice (Checkbox) {len(checkbox_questions)} ข้อ...{Style.RESET_ALL}")
            for q in checkbox_questions:
                ok = await self._fill_checkbox(q)
                if ok:
                    result['filled'] += 1
                else:
                    result['failed'].append(q.get('response_id', f'CB#{q.get("question_number")}'))
            result['total'] += len(checkbox_questions)

        # 4. Dropdown
        dd2_questions = [q for q in all_questions if q.get('type') == 'Dropdown']
        print(f"  {Fore.CYAN}[*] จะเติม Dropdown {len(dd2_questions)} ช่อง...{Style.RESET_ALL}")
        result['total'] += len(dd2_questions)
        filled_ok = 0
        failed = []
        for idx, q in enumerate(dd2_questions, 1):
            ok = await self._fill_dropdown(q)
            if ok:
                filled_ok += 1
            else:
                failed.append(q.get('response_id', f'#{idx}'))
            await self.debug_wait(0.4)

        result['filled'] += filled_ok
        result['failed'].extend(failed)

        if dd2_questions and filled_ok == 0:
            result['reason'] = f'เติม dropdown ไม่ได้เลย (0/{len(dd2_questions)})'
            print(f"  {Fore.RED}[!] {result['reason']} — ยกเลิกการกด Check{Style.RESET_ALL}")
            return result

        await self.debug_wait(2)

        success = await self._click_check_button()
        if success:
            result['success'] = True
            result['reason'] = 'OK'
            print(f"  {Fore.GREEN}[+] เติมคำตอบและกด Check สำเร็จ{Style.RESET_ALL}")
            
            try:
                body_text = await self.page.inner_text('body')
                score_match = re.search(r'You scored (\d+) out of (\d+)', body_text, re.IGNORECASE)
                if score_match:
                    result['score'] = f"{score_match.group(1)}/{score_match.group(2)}"
                    print(f"  {Fore.GREEN}[+] คะแนน: {result['score']}{Style.RESET_ALL}")
            except Exception:
                pass
                
        else:
            print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check — ลองรออีก 3 วินาที...{Style.RESET_ALL}")
            await self.debug_wait(3)
            success = await self._click_check_button()
            if success:
                result['success'] = True
                result['reason'] = 'OK (Retry Check)'
                print(f"  {Fore.GREEN}[+] เติมคำตอบและกด Check สำเร็จ (รอบ 2){Style.RESET_ALL}")
            else:
                result['reason'] = 'ไม่พบปุ่ม Check'
                print(f"  {Fore.YELLOW}[!] {result['reason']}{Style.RESET_ALL}")

        elapsed = time.time() - self.start_time
        self.exercise_times[exercise_name] = elapsed
        print(f"  {Fore.CYAN}[*] ใช้เวลาไป: {elapsed:.2f} วินาที{Style.RESET_ALL}")

        return result

    # ==================== Multi-Page ====================

    async def is_multi_question_mc(self):
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False
            result = await frame.evaluate('''
                () => {
                    const hasChoices = document.querySelectorAll('.choice_interaction, input[type="radio"]').length > 0;
                    if (!hasChoices) return false;

                    const steps = document.querySelectorAll('.progress-bar .step');
                    const stepCount = steps.length;

                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    const visibleWrapCount = visibleWraps.length;

                    if (visibleWrapCount >= 2) return false;

                    if (stepCount >= 2 && visibleWrapCount === 1) return true;

                    return false;
                }
            ''')
            return bool(result)
        except Exception:
            return False

    async def get_current_question_number(self):
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return 0

            num = await frame.evaluate('''
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
            ''')
            return num if num else 0
        except Exception:
            return 0

    async def get_current_question_text(self):
        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return {'options': [], 'index': -1, 'total': 0}

            result = await frame.evaluate('''
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
                    
                    const choices = activeWrap.querySelectorAll('.choice_interaction');
                    if (choices.length > 0) {
                        choices[0].querySelectorAll('.is-radiobutton-choice-text').forEach(t => {
                            options.push(t.innerText.trim());
                        });
                    }
                    
                    if (options.length === 0) {
                        const checkboxes = activeWrap.querySelectorAll('.input-checkbox');
                        checkboxes.forEach(cb => {
                            const textEl = cb.querySelector('.is-checkbox-choice-text');
                            if (textEl) {
                                options.push(textEl.innerText.trim());
                            }
                        });
                    }
                    
                    const total = document.querySelectorAll('.content-wrap').length;
                    
                    return {options: options, index: idx, total: total};
                }
            ''')
            return result if result else {'options': [], 'index': -1, 'total': 0}
        except Exception:
            return {'options': [], 'index': -1, 'total': 0}

    async def _fill_multiple_choice_for_current(self, q, choice_index):
        correct_text = self._normalize_text(q.get('correct_answer', ''))
        correct_answers = q.get('correct_answers', [])
        is_multiple = q.get('is_multiple_answers', False)
        
        if not correct_text and not correct_answers:
            print(f"  {Fore.YELLOW}[!] ไม่มีคำตอบ{Style.RESET_ALL}")
            return False

        print(f"  {Fore.CYAN}[*] เติม MC ที่ content_wrap_{choice_index}: '{correct_text}'{Style.RESET_ALL}")

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False

            if is_multiple and correct_answers:
                clicked = await frame.evaluate('''
                    (args) => {
                        const [correctTexts] = args;
                        let clickedCount = 0;
                        
                        const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                        if (visibleWraps.length === 0) return false;
                        
                        const activeWrap = visibleWraps[0];
                        const checkboxes = activeWrap.querySelectorAll('.input-checkbox');
                        
                        for (const cb of checkboxes) {
                            const textEl = cb.querySelector('.is-checkbox-choice-text');
                            if (!textEl) continue;
                            
                            const txt = textEl.innerText.trim();
                            
                            for (const correctText of correctTexts) {
                                if (txt === correctText || txt.includes(correctText) || correctText.includes(txt)) {
                                    const input = cb.querySelector('input[type="checkbox"]');
                                    if (input && !input.checked) {
                                        cb.click();
                                        input.checked = true;
                                        input.dispatchEvent(new Event('change', { bubbles: true }));
                                        clickedCount++;
                                    }
                                    break;
                                }
                            }
                        }
                        return clickedCount > 0;
                    }
                ''', [correct_answers])
                
                if clicked:
                    await self.debug_wait(0.8)
                    print(f"      {Fore.GREEN}[OK] ติ๊กถูกสำเร็จ{Style.RESET_ALL}")
                    return True
                return False

            clicked = await frame.evaluate('''
                (args) => {
                    const [idx, corrText] = args;
                    
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) return false;
                    
                    const activeWrap = visibleWraps[0];
                    
                    const radios = activeWrap.querySelectorAll('.input-radio');
                    for (const radio of radios) {
                        const textEl = radio.querySelector('.is-radiobutton-choice-text');
                        if (!textEl) continue;
                        const txt = textEl.innerText.trim();
                        if (txt === corrText) {
                            radio.click();
                            const input = radio.querySelector('input[type="radio"]');
                            if (input) {
                                input.checked = true;
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                    }
                    
                    for (const radio of radios) {
                        const textEl = radio.querySelector('.is-radiobutton-choice-text');
                        if (!textEl) continue;
                        const txt = textEl.innerText.trim();
                        if (txt.includes(corrText) || corrText.includes(txt)) {
                            radio.click();
                            const input = radio.querySelector('input[type="radio"]');
                            if (input) {
                                input.checked = true;
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                    }
                    
                    return false;
                }
            ''', [choice_index, correct_text])

            if clicked:
                await self.debug_wait(0.8)
                print(f"      {Fore.GREEN}[OK] เติมสำเร็จ{Style.RESET_ALL}")
                return True

            print(f"      {Fore.YELLOW}[!] หาตัวเลือกไม่เจอ{Style.RESET_ALL}")
            return False
        except Exception as e:
            print(f"  {Fore.RED}[!] _fill_multiple_choice_for_current error: {e}{Style.RESET_ALL}")
            return False

    async def _click_next_button_if_present(self, timeout=10):
        next_selectors = [
            'a[title="Next"]',
            'a.green-btn[title="Next"]',
            'a.nextActivityBtn:not([hidden])',
            'a:has-text("Next")',
            'button:has-text("Next")',
            'a.btn.green-btn',
            'button[data-event="forward"]',
            '.btn-next',
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
                        btn_title = (await btn.get_attribute('title') or '').strip()

                        is_next = (
                            btn_text.lower() == 'next' or
                            btn_title.lower() == 'next' or
                            'next' in btn_text.lower()
                        )

                        if not is_next:
                            continue

                        disabled_attr = await btn.get_attribute('disabled')
                        class_attr = (await btn.get_attribute('class') or '').lower()
                        is_disabled = (
                            disabled_attr is not None
                            or 'disabled' in class_attr
                            or 'inactive' in class_attr
                        )

                        if is_disabled:
                            continue

                        print(f"  {Fore.GREEN}[+] พบปุ่ม Next - กำลังกด...{Style.RESET_ALL}")
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
            old_options = old_data.get('options', []) if old_data else []
            old_index = old_data.get('index', -1) if old_data else -1

            print(f"  {Fore.CYAN}[*] รอหน้าเปลี่ยน (old_num={old_num}, old_idx={old_index}, opts={len(old_options)})...{Style.RESET_ALL}")

            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                new_num = await self.get_current_question_number()
                new_data = await self.get_current_question_text()
                new_options = new_data.get('options', []) if new_data else []
                new_index = new_data.get('index', -1) if new_data else -1

                if expected_next_num is not None:
                    if new_num == expected_next_num:
                        print(f"  {Fore.GREEN}[+] ถึงข้อที่คาดหวัง: {new_num}{Style.RESET_ALL}")
                        return True
                else:
                    if new_num != old_num:
                        print(f"  {Fore.GREEN}[+] progress เปลี่ยน: {old_num} -> {new_num}{Style.RESET_ALL}")
                        return True
                    if new_index != old_index:
                        print(f"  {Fore.GREEN}[+] index เปลี่ยน: {old_index} -> {new_index}{Style.RESET_ALL}")
                        return True
                    if new_options and old_options and new_options != old_options:
                        print(f"  {Fore.GREEN}[+] ตัวเลือกเปลี่ยน{Style.RESET_ALL}")
                        return True

                await asyncio.sleep(0.5)
            return False
        except Exception:
            return False

    async def continue_multi_question(self, exercise_name, answers_by_file, max_iterations=25):
        print(f"\n  {Fore.CYAN}[*] เริ่มทำ Multi-Page ต่อจนจบ...{Style.RESET_ALL}")

        frame = await self._get_activity_frame(timeout=10)
        if not frame:
            print(f"  {Fore.RED}[!] ไม่พบ activity frame{Style.RESET_ALL}")
            return False

        all_questions = []
        if answers_by_file:
            for _, questions in answers_by_file.items():
                all_questions.extend(questions)

        mc_questions = [q for q in all_questions if q.get('type') == 'Multiple Choice']
        if not mc_questions:
            print(f"  {Fore.YELLOW}[!] ไม่มี MC{Style.RESET_ALL}")
            return False

        total_questions = len(mc_questions)
        print(f"  {Fore.CYAN}[*] มี MC ทั้งหมด {total_questions} ข้อ{Style.RESET_ALL}")

        used_answers = set()

        for iteration in range(total_questions):
            print(f"\n  {Fore.CYAN}[*] === รอบที่ {iteration + 1}/{total_questions} ==={Style.RESET_ALL}")

            current_url = self.page.url
            if '/view/' not in current_url and '/activity/' not in current_url and 'item' not in current_url:
                print(f"  {Fore.GREEN}[+] ออกจากหน้าแบบฝึกหัดแล้ว — จบ{Style.RESET_ALL}")
                return True

            current_num_before = await self.get_current_question_number()
            current_data = await self.get_current_question_text()
            current_options = current_data.get('options', []) if current_data else []
            current_index = current_data.get('index', -1) if current_data else -1

            print(f"  {Fore.CYAN}[*] ข้อปัจจุบัน: num={current_num_before}, index={current_index}{Style.RESET_ALL}")
            print(f"  {Fore.CYAN}[*] ตัวเลือกในหน้า: {current_options}{Style.RESET_ALL}")

            if not current_options or current_index < 0:
                print(f"  {Fore.YELLOW}[!] ไม่พบตัวเลือก — อาจจบแล้ว{Style.RESET_ALL}")
                return True

            target_q = None
            for q in mc_questions:
                correct_ans = self._normalize_text(q.get('correct_answer', ''))
                if correct_ans in current_options and correct_ans not in used_answers:
                    target_q = q
                    break

            if not target_q:
                for q in mc_questions:
                    correct_ans = self._normalize_text(q.get('correct_answer', ''))
                    if correct_ans and correct_ans not in used_answers:
                        target_q = q
                        break

            if not target_q:
                print(f"  {Fore.YELLOW}[!] ไม่พบคำตอบที่ตรง — อาจทำครบแล้ว{Style.RESET_ALL}")
                return True

            correct_text = self._normalize_text(target_q.get('correct_answer', ''))
            used_answers.add(correct_text)

            print(f"  {Fore.CYAN}[*] เลือกคำตอบ: '{correct_text}'{Style.RESET_ALL}")

            ok = await self._fill_multiple_choice_for_current(target_q, current_index)
            if not ok:
                print(f"  {Fore.RED}[!] เติมไม่สำเร็จ — หยุด{Style.RESET_ALL}")
                return False

            await self.debug_wait(1.5)

            print(f"  {Fore.CYAN}[*] กด Check...{Style.RESET_ALL}")
            success = await self._click_check_button()
            if not success:
                print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check — อาจจบแล้ว{Style.RESET_ALL}")
                return True

            await self.debug_wait(2)

            if iteration + 1 == total_questions:
                print(f"  {Fore.GREEN}[+] ทำข้อสุดท้าย ({total_questions}/{total_questions}) เรียบร้อยแล้ว{Style.RESET_ALL}")
                return True

            print(f"  {Fore.CYAN}[*] กดปุ่ม Next (หลัง Check)...{Style.RESET_ALL}")
            next_clicked = await self._click_next_button_if_present(timeout=10)

            if not next_clicked:
                print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Next — อาจเป็นข้อสุดท้าย{Style.RESET_ALL}")
                return True

            await self.debug_wait(1.5)

            expected_next = current_num_before + 1 if current_num_before > 0 else None
            print(f"  {Fore.CYAN}[*] รอหน้าเปลี่ยนไปข้อ {expected_next}...{Style.RESET_ALL}")

            changed = await self.wait_for_next_question(timeout=15, expected_next_num=expected_next)

            if not changed:
                new_num = await self.get_current_question_number()
                if new_num > current_num_before:
                    print(f"  {Fore.GREEN}[+] progress เพิ่มขึ้นเป็น {new_num} — ถือว่าเปลี่ยนแล้ว{Style.RESET_ALL}")
                else:
                    print(f"  {Fore.YELLOW}[!] หน้าไม่เปลี่ยน — อาจเป็นข้อสุดท้าย{Style.RESET_ALL}")
                    return True

            print(f"  {Fore.GREEN}[+] หน้าเปลี่ยนแล้ว — ไปข้อถัดไป{Style.RESET_ALL}")
            await self.debug_wait(2)

        print(f"  {Fore.GREEN}[+] ทำ Multi-Page จบแล้ว ({total_questions} รอบ){Style.RESET_ALL}")
        return True

    # ==================== Speaking Activity ====================

    async def handle_speaking_activity(self):
        print(f"  {Fore.CYAN}[*] กำลังตรวจสอบ Speaking Activity...{Style.RESET_ALL}")

        skip_count = 0
        max_skip = 20

        while skip_count < max_skip:
            skip_btn = await self._find_skip_button()
            if skip_btn:
                print(f"  {Fore.YELLOW}[+] พบปุ่ม Skip — กดข้าม! ({skip_count + 1}){Style.RESET_ALL}")
                try:
                    await skip_btn.scroll_into_view_if_needed()
                    await skip_btn.click(force=True)
                    skip_count += 1
                    await self.debug_wait(3)
                    continue
                except Exception as e:
                    print(f"  {Fore.RED}[!] กด Skip ไม่ได้: {e}{Style.RESET_ALL}")

            next_btn = await self._find_next_activity_button()
            if next_btn:
                print(f"  {Fore.GREEN}[+] พบปุ่ม NEXT ACTIVITY — กดผ่าน...{Style.RESET_ALL}")
                try:
                    await next_btn.scroll_into_view_if_needed()
                    await next_btn.click(force=True)
                except Exception:
                    pass
                await self.debug_wait(3)
                await self.safe_wait_for_load()
                return True

            try:
                body_text = await self.page.inner_text('body')
                if 'NEXT ACTIVITY' in body_text or 'Next activity' in body_text:
                    print(f"  {Fore.GREEN}[+] พบข้อความ NEXT ACTIVITY — จบแล้ว{Style.RESET_ALL}")
                    return True
            except Exception:
                pass

            if skip_count == 0:
                return False

            await self.debug_wait(2)

        print(f"  {Fore.YELLOW}[!] กด Skip ครบแล้ว — ลองหาปุ่ม Speak{Style.RESET_ALL}")
        speak_btn = None
        for frame in self.page.frames:
            try:
                btn = await frame.query_selector('button:has-text("Speak"), button:has-text("Try again")')
                if btn and await btn.is_visible():
                    speak_btn = btn
                    break
            except Exception:
                continue
        
        if speak_btn:
            print(f"  {Fore.CYAN}[*] พบปุ่ม Speak — กดเพื่อไปต่อ{Style.RESET_ALL}")
            try:
                await speak_btn.click(force=True)
                await self.debug_wait(3)
                return True
            except Exception:
                pass

        return True

    # ==================== Fill Functions ====================

    async def _fill_multiple_choice_by_index(self, q, question_index):
        correct_text = self._normalize_text(q.get('correct_answer', ''))
        if not correct_text:
            return False

        print(f"  {Fore.CYAN}[*] MC ข้อ {q.get('question_number', '?')} (index {question_index}): เลือก '{correct_text}'{Style.RESET_ALL}")

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False

            clicked = await frame.evaluate('''
                (args) => {
                    const [qIndex, corrText] = args;
                    
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) return false;
                    
                    const activeWrap = visibleWraps[0];
                    
                    const choices = activeWrap.querySelectorAll('.choice_interaction');
                    if (choices.length === 0) return false;
                    
                    let targetChoice = null;
                    if (qIndex < choices.length) {
                        targetChoice = choices[qIndex];
                    } else if (choices.length > 0) {
                        targetChoice = choices[0];
                    }
                    
                    if (!targetChoice) return false;
                    
                    const radios = targetChoice.querySelectorAll('.input-radio');
                    for (const radio of radios) {
                        const textEl = radio.querySelector('.is-radiobutton-choice-text');
                        if (!textEl) continue;
                        const txt = textEl.innerText.trim();
                        if (txt === corrText) {
                            radio.click();
                            const input = radio.querySelector('input[type="radio"]');
                            if (input) {
                                input.checked = true;
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                    }
                    
                    for (const radio of radios) {
                        const textEl = radio.querySelector('.is-radiobutton-choice-text');
                        if (!textEl) continue;
                        const txt = textEl.innerText.trim();
                        if (txt.includes(corrText) || corrText.includes(txt)) {
                            radio.click();
                            const input = radio.querySelector('input[type="radio"]');
                            if (input) {
                                input.checked = true;
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                            return true;
                        }
                    }
                    
                    return false;
                }
            ''', [question_index, correct_text])

            if clicked:
                await self.debug_wait(0.8)
                print(f"      {Fore.GREEN}[OK] เติมสำเร็จ{Style.RESET_ALL}")
                return True

            print(f"      {Fore.YELLOW}[!] หาตัวเลือกไม่เจอ{Style.RESET_ALL}")
            return False
        except Exception as e:
            print(f"  {Fore.RED}[!] error: {e}{Style.RESET_ALL}")
            return False

    async def _fill_checkbox(self, q):
        correct_answers = q.get('correct_answers', [])
        if not correct_answers:
            return False

        question_num = q.get('question_number', '?')
        print(f"  {Fore.CYAN}[*] Checkbox ข้อ {question_num}: ต้องเลือก {len(correct_answers)} ข้อ{Style.RESET_ALL}")

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False

            filled = await frame.evaluate('''
                (args) => {
                    const correctTexts = args;
                    let clickedCount = 0;
                    
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) return false;
                    
                    const activeWrap = visibleWraps[0];
                    const checkboxes = activeWrap.querySelectorAll('.input-checkbox');
                    
                    for (const cb of checkboxes) {
                        const textEl = cb.querySelector('.is-checkbox-choice-text');
                        if (!textEl) continue;
                        
                        const txt = textEl.innerText.trim();
                        
                        for (const correctText of correctTexts) {
                            if (txt === correctText || txt.includes(correctText) || correctText.includes(txt)) {
                                const input = cb.querySelector('input[type="checkbox"]');
                                if (input && !input.checked) {
                                    cb.click();
                                    input.checked = true;
                                    input.dispatchEvent(new Event('change', { bubbles: true }));
                                    clickedCount++;
                                }
                                break;
                            }
                        }
                    }
                    return clickedCount > 0;
                }
            ''', correct_answers)

            if filled:
                await self.debug_wait(0.8)
                print(f"      {Fore.GREEN}[OK] ติ๊กถูกสำเร็จ{Style.RESET_ALL}")
                return True
            return False
        except Exception:
            return False

    async def _fill_dropdown(self, q):
        correct_text_raw = q.get('correct_answer', '')
        response_id = q.get('response_id', '')
        question_num = q.get('question_number', '?')

        if not correct_text_raw:
            return False

        correct_text_norm = self._normalize_text(correct_text_raw)
        print(f"  {Fore.CYAN}[*] ข้อ {question_num} ({response_id}): เติม '{correct_text_norm}'{Style.RESET_ALL}")

        try:
            target_frame = None
            wrapper = None

            if response_id:
                for frame in self.page.frames:
                    try:
                        w = await frame.query_selector(
                            f'[data-response-identifier="{response_id}"].wrapper-dropdown, '
                            f'[data-response-identifier="{response_id}"].listbox'
                        )
                        if w:
                            target_frame = frame
                            wrapper = w
                            break
                    except Exception:
                        continue

            if wrapper is None:
                try:
                    target_idx = int(response_id.replace('RESPONSE', '')) if response_id else (question_num - 1)
                except Exception:
                    target_idx = question_num - 1

                print(f"  {Fore.CYAN}[*] DEBUG: target_idx={target_idx}{Style.RESET_ALL}")

                for frame in self.page.frames:
                    try:
                        all_wrappers = await frame.query_selector_all(
                            '.wrapper-dropdown.listbox, .wrapper-dropdown'
                        )
                        if not all_wrappers:
                            continue
                        print(f"  {Fore.CYAN}[*] DEBUG: total_wrappers={len(all_wrappers)}{Style.RESET_ALL}")
                        if target_idx < len(all_wrappers):
                            wrapper = all_wrappers[target_idx]
                            target_frame = frame
                            break
                    except Exception:
                        continue

            if wrapper is None:
                for frame in self.page.frames:
                    try:
                        all_wrappers = await frame.query_selector_all('.wrapper-dropdown.listbox')
                        for w in all_wrappers:
                            btn = await w.query_selector('button.drop-label')
                            if btn:
                                txt = (await btn.inner_text()).strip()
                                if not txt:
                                    wrapper = w
                                    target_frame = frame
                                    break
                        if wrapper:
                            break
                    except Exception:
                        continue

            if wrapper is None or target_frame is None:
                print(f"      {Fore.YELLOW}[!] หา wrapper ไม่เจอ{Style.RESET_ALL}")
                return False

            toggle = await wrapper.query_selector(
                'button.drop-label, button.listbox__label, button[role="combobox"], button'
            )
            if toggle is None:
                return False

            try:
                await target_frame.evaluate("document.activeElement && document.activeElement.blur()")
            except Exception:
                pass

            try:
                await toggle.scroll_into_view_if_needed()
                await self.debug_wait(0.2)

                box = await toggle.bounding_box()
                if box:
                    await self.page.mouse.click(
                        box['x'] + box['width'] / 2,
                        box['y'] + box['height'] / 2
                    )
                else:
                    await toggle.click(force=True)
            except Exception:
                return False

            opened = False
            for _ in range(20):
                try:
                    aria = await toggle.get_attribute('aria-expanded')
                    if aria == 'true':
                        opened = True
                        break
                except Exception:
                    pass
                await self.debug_wait(0.1)

            if not opened:
                try:
                    await toggle.click(force=True)
                    await self.debug_wait(0.3)
                    aria = await toggle.get_attribute('aria-expanded')
                    if aria != 'true':
                        return False
                except Exception:
                    return False

            popup = await wrapper.query_selector(
                '.popup.listbox__popup, .listbox__popup, .popup, ul.listbox__choices'
            )
            items = []
            if popup:
                items = await popup.query_selector_all(
                    'li.listbox__choice, li[role="option"], li'
                )

            if not items:
                try:
                    await target_frame.evaluate("document.body.click()")
                except Exception:
                    pass
                return False

            matched = None
            clean_str = lambda s: re.sub(r'[^a-zA-Z0-9]', '', s).lower()
            target_clean = clean_str(correct_text_norm)

            for item in items:
                try:
                    txt = (await item.inner_text()).strip()
                    txt_norm = self._normalize_text(txt)
                    
                    if txt_norm == correct_text_norm:
                        matched = item
                        break
                    
                    if clean_str(txt_norm) == target_clean:
                        matched = item
                        break
                except Exception:
                    continue

            if matched is None:
                for item in items:
                    try:
                        txt = (await item.inner_text()).strip()
                        txt_norm = self._normalize_text(txt)
                        if correct_text_norm in txt_norm or txt_norm in correct_text_norm:
                            matched = item
                            break
                    except Exception:
                        continue

            if matched is None:
                return False

            try:
                await matched.scroll_into_view_if_needed()
                await matched.hover()
                await self.debug_wait(0.15)

                box = await matched.bounding_box()
                if box:
                    await self.page.mouse.click(
                        box['x'] + box['width'] / 2,
                        box['y'] + box['height'] / 2
                    )
                else:
                    await matched.click(force=True)

                await self.debug_wait(0.4)
                print(f"      {Fore.GREEN}[OK] เติม '{correct_text_norm}' สำเร็จ{Style.RESET_ALL}")
                return True
            except Exception:
                return False

        except Exception as e:
            print(f"  {Fore.RED}[!] _fill_dropdown error: {e}{Style.RESET_ALL}")
            return False

    async def _fill_text_entry(self, q):
        correct_text = self._normalize_text(q.get('correct_answer', ''))
        if not correct_text:
            return False

        response_id = q.get('response_id', '')
        question_num = q.get('question_number', '?')
        
        print(f"  {Fore.CYAN}[*] Text Entry ข้อ {question_num}: เติม '{correct_text}'{Style.RESET_ALL}")

        try:
            frame = await self._get_activity_frame(timeout=3)
            if not frame:
                return False

            filled = await frame.evaluate('''
                (args) => {
                    const [correctText, respId] = args;
                    
                    const visibleWraps = document.querySelectorAll('.content-wrap:not(.activity--hidden)');
                    if (visibleWraps.length === 0) return false;
                    
                    const activeWrap = visibleWraps[0];
                    
                    const inputs = activeWrap.querySelectorAll('div.input-text > input[type="text"], input[type="text"]');
                    
                    for (const input of inputs) {
                        if (input.value && input.value.trim() !== '') continue;
                        
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        )?.set;
                        
                        if (nativeInputValueSetter) {
                            nativeInputValueSetter.call(input, correctText);
                        } else {
                            input.value = correctText;
                        }
                        
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new Event('keyup', { bubbles: true }));
                        input.dispatchEvent(new Event('blur', { bubbles: true }));
                        
                        input.focus();
                        input.blur();
                        
                        return true;
                    }
                    
                    return false;
                }
            ''', [correct_text, response_id])

            if filled:
                await self.debug_wait(0.8)
                print(f"      {Fore.GREEN}[OK] เติมสำเร็จ{Style.RESET_ALL}")
                return True
            return False
        except Exception:
            return False

    async def _fill_click_to_fill(self, q):
        """
        เติมคำตอบแบบ Click-to-fill
        วิธีทำงาน: คลิกที่ปุ่ม <button> ที่อยู่ใน li.drop_area ของ pool
        โดยใช้ JS หา <button> ที่ aria-labelledby ตรงกับ content-id ของคำศัพท์
        """
        pairs = q.get('correct_pairs', {})
        if not pairs:
            print(f"  {Fore.YELLOW}[!] ไม่มี correct_pairs สำหรับ Click-to-fill{Style.RESET_ALL}")
            return False

        # เรียงตาม gap_id (ลำดับช่องว่าง)
        ordered_pairs = sorted(pairs.items(), key=lambda x: x[0])
        success_count = 0
        
        print(f"  {Fore.CYAN}[*] กำลังคลิกคำตอบจำนวน {len(ordered_pairs)} ข้อ...{Style.RESET_ALL}")

        for idx, (gap_id, answer_text_raw) in enumerate(ordered_pairs, 1):
            answer_text = self._normalize_text(answer_text_raw)
            
            success = await self._click_answer_button(answer_text, gap_id, idx)
            
            if success:
                success_count += 1
                print(f"      {Fore.GREEN}[OK] คลิก '{answer_text}' สำเร็จ{Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}[!] คลิก '{answer_text}' ไม่สำเร็จ{Style.RESET_ALL}")
            
            await self.debug_wait(0.8)  # รอให้ระบบประมวลผล

        return success_count == len(ordered_pairs)

    async def _click_answer_button(self, answer_text, gap_id, order_num, max_retries=2):
        """
        คลิกปุ่มคำตอบใน pool แบบ Click-to-fill
        Cambridge One จะย้ายคำตอบไป gap ที่ว่างถัดไปให้อัตโนมัติ
        (ใช้ logic เดียวกับ old.py ที่ทดสอบแล้วว่าทำงานได้)
        """
        clean_text = answer_text.strip().lower()

        for attempt in range(max_retries):
            # === กลยุทธ์ 1: XPath หา element ที่มีข้อความตรง ===
            for frame in self.page.frames:
                try:
                    selector = (
                        f'xpath=//*[(self::button or self::div or self::li or self::span) '
                        f'and normalize-space(translate(text(), '
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

            # === กลยุทธ์ 2: JS click (fallback) ===
            for frame in self.page.frames:
                try:
                    js_clicked = await frame.evaluate('''
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
                    ''', answer_text)
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
            '.check-button',
            '.check-btn',
            '.activity-check-button',
            'a.green-btn',
            'a.btn.green-btn',
            'div.green-btn',
            'a:has-text("Check")',
            'button:has-text("Check")',
            'button:has-text("Submit")',
            'a:has-text("Submit")',
            '.submit-btn',
            'button[type="submit"]',
            '[data-action="submit"]',
            '.nemo-button-primary',
        ]

        for attempt in range(30):
            # Activity iframe อาจถูก reload หลังเติมคำตอบ จึงต้องอ่าน frames ใหม่ทุกครั้ง
            frames_to_search = list(self.page.frames)
            for frame in frames_to_search:
                for selector in check_selectors:
                    try:
                        buttons = await frame.query_selector_all(selector)
                        for btn in buttons:
                            if not await btn.is_visible():
                                continue

                            btn_text = (await btn.inner_text()).strip()
                            btn_title = (await btn.get_attribute('title') or '').strip()
                            aria_label = (await btn.get_attribute('aria-label') or '').strip()
                            action = (await btn.get_attribute('data-action') or '').strip().lower()
                            event = (await btn.get_attribute('data-event') or '').strip().lower()
                            class_attr = (await btn.get_attribute('class') or '').lower()

                            semantic_check = any(
                                'check' in value.lower()
                                for value in (btn_text, btn_title, aria_label, action, event)
                                if value
                            )
                            class_only_selector = selector in {
                                '.check-button',
                                '.check-btn',
                                '.activity-check-button',
                                '.submit-btn',
                                'button[type="submit"]',
                            }
                            if not semantic_check and not class_only_selector:
                                continue

                            disabled_attr = await btn.get_attribute('disabled')
                            aria_disabled = (await btn.get_attribute('aria-disabled') or '').lower()
                            is_disabled = (
                                disabled_attr is not None
                                or aria_disabled == 'true'
                                or 'disabled' in class_attr
                                or 'inactive' in class_attr
                            )
                            if is_disabled:
                                continue

                            label = btn_text or aria_label or btn_title or selector
                            print(f"  {Fore.GREEN}[+] พบปุ่ม '{label}' - กำลังกด...{Style.RESET_ALL}")
                            await btn.scroll_into_view_if_needed()
                            await btn.click(force=True)
                            await self.debug_wait(2)
                            return True
                    except Exception:
                        continue
            await asyncio.sleep(0.5)

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Check{Style.RESET_ALL}")
        return False

    async def check_and_retry_if_needed(self, exercise_name, answers_by_file):
        print(f"\n  {Fore.CYAN}[*] กำลังตรวจสอบผลลัพธ์...{Style.RESET_ALL}")
        await self.debug_wait(2)

        try:
            body = await self.page.inner_text('body')
            body_lower = body.lower()

            has_try_again = 'try again' in body_lower or 'retry' in body_lower
            has_check_again = 'check again' in body_lower

            if has_try_again or has_check_again:
                print(f"  {Fore.YELLOW}[!] ตรวจพบว่าตอบผิด - กำลัง Retry{Style.RESET_ALL}")
                retried = await self.retry_exercise(exercise_name)
                if not retried:
                    await self.reset_exercise(exercise_name)

                await self.debug_wait(2)

                print(f"  {Fore.CYAN}[*] กำลังเติมคำตอบใหม่...{Style.RESET_ALL}")
                await self.auto_fill_and_check(exercise_name, answers_by_file)
                return True

            if 'correct' in body_lower or 'next' in body_lower or 'amazing' in body_lower:
                print(f"  {Fore.GREEN}[+] ตรวจพบว่าตอบถูกแล้ว{Style.RESET_ALL}")
                return True

        except Exception:
            pass

        return False

    async def retry_exercise(self, exercise_name):
        print(f"\n  {Fore.CYAN}[*] กำลังลอง Retry แบบฝึกหัด: {exercise_name}{Style.RESET_ALL}")

        retry_selectors = [
            'button:has-text("Try again")',
            'button:has-text("Retry")',
            'button:has-text("Try Again")',
            'a:has-text("Try again")',
            'a:has-text("Retry")',
            '.retry-btn',
            '.try-again',
            '[data-action="retry"]',
            'button[data-event="retry"]',
        ]

        for selector in retry_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip()
                    print(f"  {Fore.GREEN}[+] พบปุ่ม '{btn_text}' - กำลังกด...{Style.RESET_ALL}")
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await self.debug_wait(2)
                    return True
            except Exception:
                continue

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Retry{Style.RESET_ALL}")
        return False

    async def reset_exercise(self, exercise_name):
        print(f"\n  {Fore.CYAN}[*] กำลัง Reset แบบฝึกหัด: {exercise_name}{Style.RESET_ALL}")

        reset_selectors = [
            'button:has-text("Reset")',
            'button:has-text("Start again")',
            'a:has-text("Reset")',
            '.reset-btn',
            '[data-action="reset"]',
            'button[data-event="reset"]',
            'button[data-event="reset_all"]',
        ]

        for selector in reset_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    btn_text = (await btn.inner_text()).strip()
                    print(f"  {Fore.GREEN}[+] พบปุ่ม '{btn_text}' - กำลังกด...{Style.RESET_ALL}")
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
            'a:has-text("Next")', 'button:has-text("Next")',
            '.green-btn', '[qid="next-btn"]', 'a.btn.green-btn',
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
                await self.page.evaluate('''
                    () => {
                        const btns = document.querySelectorAll('a, button');
                        for (const b of btns) {
                            if ((b.innerText || '').trim().toLowerCase() === 'next') {
                                b.click();
                                return true;
                            }
                        }
                    }
                ''')
            except Exception:
                pass

        await self.debug_wait(2)
        await self.safe_wait_for_load()

        score_text = ""
        try:
            body_text = await self.page.inner_text('body')
            match = re.search(r'You scored\s+(\d+\s+out of\s+\d+)', body_text, re.IGNORECASE)
            if match:
                score_text = match.group(0)
                print(f"  {Fore.GREEN}[+] ส่งงานสำเร็จ! คะแนน: {score_text}{Style.RESET_ALL}")
            else:
                print(f"  {Fore.GREEN}[+] ส่งงานเรียบร้อยแล้ว{Style.RESET_ALL}")
        except Exception:
            pass

        print(f"\n  {Fore.CYAN}{'=' * 50}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}✓ ทำแบบฝึกหัดเสร็จสมบูรณ์ ({score_text}){Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 50}{Style.RESET_ALL}")
        print(f"  [1] ทำข้อต่อไป (Next activity)")
        print(f"  [2] ทำข้อนี้ใหม่ (Start again)")
        print(f"  [3] กลับไปหน้าเลือกแบบฝึกหัด")
        print(f"  [4] แสดง log file path")
        print(f"  [0] ปิดโปรแกรม")

        try:
            choice = input(f"\n  {Fore.CYAN}[>] เลือกการกระทำ (0-4): {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            choice = '3'

        if choice == '1':
            print(f"  {Fore.CYAN}[*] กำลังไปยังข้อถัดไป...{Style.RESET_ALL}")
            try:
                await self.page.click('a[qid="resultScreen-1"], .btn-primary:has-text("Next activity")', force=True)
                await self.safe_wait_for_load()
                await self.debug_wait(2)

                new_ex_name = await self.page.evaluate('''
                    () => {
                        const titleEl = document.querySelector('.activity-title, h1, .product-title, header span, .header-title');
                        return titleEl ? titleEl.innerText.trim() : '';
                    }
                ''')
                return ('next', new_ex_name)
            except Exception:
                pass
            return ('next', None)

        elif choice == '2':
            print(f"  {Fore.CYAN}[*] กำลังเริ่มทำข้อนี้ใหม่...{Style.RESET_ALL}")
            try:
                await self.page.click('a[qid="resultScreen-2"], .tryAgain-btn', force=True)
                await self.safe_wait_for_load()
            except Exception:
                pass
            return ('retry', None)

        elif choice == '4':
            print(f"\n  {Fore.CYAN}Log file: {self.log_file_path or 'ยังไม่เริ่ม'}{Style.RESET_ALL}")
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass
            return ('retry', None)

        elif choice == '0':
            print(f"  {Fore.CYAN}[*] กำลังปิดโปรแกรม...{Style.RESET_ALL}")
            return ('close', None)

        else:
            print(f"  {Fore.CYAN}[*] กำลังกลับสู่หน้าหลัก...{Style.RESET_ALL}")
            await self.page.go_back()
            await self.safe_wait_for_load()
            return ('back', None)

    async def submit_single_page_activity(self):
        print(f"  {Fore.CYAN}[*] กำลังส่งแบบฝึกหัด (กดปุ่ม Next แถบสีเขียว)...{Style.RESET_ALL}")
        await self.debug_wait(1.5)

        next_selectors = [
            'div.green-btn:has-text("Next")',
            'a.green-btn:has-text("Next")',
            'button.green-btn:has-text("Next")',
            'div[class*="green-btn"]',
            'a[title="Next"]',
            'button:has-text("Next")',
            'a:has-text("Next")'
        ]

        for selector in next_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    print(f"  {Fore.GREEN}[+] พบปุ่มส่งงาน Next - กำลังกด...{Style.RESET_ALL}")
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await self.safe_wait_for_load()
                    await self.debug_wait(2)
                    return True
            except Exception:
                continue

        try:
            clicked = await self.page.evaluate('''
                () => {
                    const elements = Array.from(document.querySelectorAll('a, button, div'));
                    for (const el of elements) {
                        const txt = (el.innerText || '').trim().toLowerCase();
                        if (txt === 'next' && el.offsetHeight > 0) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
            ''')
            if clicked:
                print(f"  {Fore.GREEN}[+] กดปุ่มส่งงาน Next ผ่าน JS สำเร็จ{Style.RESET_ALL}")
                await self.safe_wait_for_load()
                await self.debug_wait(2)
                return True
        except Exception:
            pass

        print(f"  {Fore.YELLOW}[!] ไม่พบปุ่ม Next สำหรับส่งงาน{Style.RESET_ALL}")
        return False

    # ==================== Browser Init ====================

    async def init_browser(self, profile_dir=None):
        self.playwright = await async_playwright().start()
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = Path(profile_dir or (self.profiles_dir / "default"))
        profile_dir.mkdir(parents=True, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir.resolve()),
            headless=False,
            viewport=None,
            permissions=['microphone'],
            args=[
                '--start-maximized',
                '--use-fake-ui-for-media-stream',
                '--use-fake-device-for-media-stream'
            ]
        )
        self.browser = self.context.browser
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("response", self._on_response)
        self.active_profile_dir = profile_dir

    def _on_response(self, response):
        try:
            url = response.url
            if 'data.js' in url:
                if url not in self.data_js_urls:
                    self.data_js_urls.append(url)
                    self.last_data_js_url = url
                    print(f"  {Fore.MAGENTA}[DATA.JS] ดัก URL ได้: {url[:120]}{Style.RESET_ALL}")
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
            content = body.decode('utf-8', errors='replace')
            self.data_js_contents[url] = content
            print(f"  {Fore.MAGENTA}[DATA.JS] ดาวน์โหลด content สำเร็จ: {len(content)} bytes{Style.RESET_ALL}")
        except Exception as e:
            print(f"  {Fore.YELLOW}[DATA.JS] โหลด content ไม่ได้: {e}{Style.RESET_ALL}")

    async def _wait_for_data_js(self, timeout=20):
        if self.data_js_urls:
            all_have_content = all(u in self.data_js_contents for u in self.data_js_urls)
            if all_have_content:
                print(f"  {Fore.GREEN}[DATA.JS] มีข้อมูลพร้อมใช้งานอยู่แล้ว: {len(self.data_js_urls)} URL{Style.RESET_ALL}")
                return True

        print(f"  {Fore.CYAN}[DATA.JS] เริ่มรอ data.js (timeout={timeout}s)...{Style.RESET_ALL}")
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            if self.data_js_urls:
                all_have_content = all(u in self.data_js_contents for u in self.data_js_urls)
                if all_have_content:
                    print(f"  {Fore.GREEN}[DATA.JS] พร้อมใช้งาน: {len(self.data_js_urls)} URL{Style.RESET_ALL}")
                    return True
            await asyncio.sleep(0.3)

        if self.data_js_urls:
            print(f"  {Fore.YELLOW}[DATA.JS] timeout แต่มี URL อยู่ {len(self.data_js_urls)}{Style.RESET_ALL}")
            return True
        print(f"  {Fore.RED}[DATA.JS] timeout — ไม่พบ data.js{Style.RESET_ALL}")
        return False

    async def check_login_status(self):
        print(f"  {Fore.CYAN}[*] กำลังตรวจสอบ session...{Style.RESET_ALL}")
        try:
            await self.page.goto('https://www.cambridgeone.org/dashboard/learner/dashboard', wait_until='domcontentloaded', timeout=20000)
        except Exception:
            pass

        await self.debug_wait(2)

        try:
            await self.page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            pass

        current_url = self.page.url.lower()
        print(f"  {Fore.CYAN}[*] URL ปัจจุบัน: {current_url}{Style.RESET_ALL}")

        if '/login' in current_url:
            print(f"  {Fore.YELLOW}[!] Session หมดอายุ{Style.RESET_ALL}")
            return False

        try:
            login_form = await self.page.query_selector('input[type="password"], input[name="password"], .gigya-input-password')
            if login_form and await login_form.is_visible():
                print(f"  {Fore.YELLOW}[!] Session หมดอายุ (พบ form login){Style.RESET_ALL}")
                return False
        except Exception:
            pass

        if 'dashboard' in current_url or '/learner/' in current_url:
            try:
                body = await self.page.inner_text('body')
                body_lower = body.lower()
                if 'my classes' in body_lower or 'active classes' in body_lower:
                    print(f"  {Fore.GREEN}[+] Session ยังใช้งานได้{Style.RESET_ALL}")
                    return True
            except Exception:
                pass
            print(f"  {Fore.GREEN}[+] Session ยังใช้งานได้{Style.RESET_ALL}")
            return True

        return False

    async def login(self, email, password):
        print(f"\n  {Fore.CYAN}[*] Logging in...{Style.RESET_ALL}")
        await self.page.goto('https://www.cambridgeone.org/login')
        await self.safe_wait_for_load()
        await self.debug_wait(1)

        email_field = None
        email_selectors = [
            'input[type="text"][name="username"]', 'input[type="text"][name="loginID"]',
            'input[type="email"]', 'input.gigya-input-text', 'input[data-gigya-name="loginID"]',
            'input[id*="loginID"]', 'input[id*="login"]', 'input[placeholder*="username"]',
            'input[placeholder*="email"]', 'input[placeholder*="Username"]', 'input[placeholder*="Email"]'
        ]

        for selector in email_selectors:
            try:
                email_field = await self.page.query_selector(selector)
                if email_field and await email_field.is_visible() and await email_field.is_enabled():
                    break
                email_field = None
            except Exception:
                email_field = None
                continue

        if not email_field:
            print(f"  {Fore.RED}[-] Could not find email field{Style.RESET_ALL}")
            return False

        await email_field.click()
        await email_field.fill('')
        await email_field.type(email, delay=20)
        await email_field.press('Tab')
        await self.debug_wait(0.5)

        password_field = None
        password_selectors = [
            'input[type="password"]', 'input.gigya-input-password', 'input[data-gigya-name="password"]',
            'input[id*="password"]', 'input[name="password"]', 'input[placeholder*="password"]',
            'input[placeholder*="Password"]'
        ]

        for selector in password_selectors:
            try:
                password_field = await self.page.query_selector(selector)
                if password_field and await password_field.is_visible() and await password_field.is_enabled():
                    break
                password_field = None
            except Exception:
                password_field = None
                continue

        if not password_field:
            print(f"  {Fore.RED}[-] Could not find password field{Style.RESET_ALL}")
            return False

        await password_field.click()
        await password_field.fill('')
        await password_field.type(password, delay=20)
        await password_field.press('Enter')
        await self.debug_wait(3)

        try:
            await self.page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass

        current_url = self.page.url
        if 'dashboard' in current_url or 'home' in current_url or 'classes' in current_url:
            print(f"  {Fore.GREEN}[+] Login successful!{Style.RESET_ALL}")
            return True

        try:
            page_text = await self.page.inner_text('body')
            if 'Active classes' in page_text or 'My classes' in page_text:
                print(f"  {Fore.GREEN}[+] Login successful!{Style.RESET_ALL}")
                return True
        except Exception:
            pass

        print(f"  {Fore.RED}[-] Login failed{Style.RESET_ALL}")
        return False

    async def get_courses_with_workbooks(self):
        print(f"\n  {Fore.CYAN}[*] Fetching courses with Digital Workbooks...{Style.RESET_ALL}")
        current_url = self.page.url
        if 'dashboard' not in current_url:
            await self.page.goto('https://www.cambridgeone.org/dashboard/learner/dashboard')
            await self.safe_wait_for_load()
            await self.debug_wait(1)

        try:
            await self.page.wait_for_selector('.umbrella-tile', timeout=12000)
        except Exception:
            pass

        courses_data = await self.page.evaluate('''
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
        ''')
        print(f"  {Fore.GREEN}[+] พบ {len(courses_data)} คอร์ส{Style.RESET_ALL}")
        return courses_data

    async def click_workbook(self, course_name):
        print(f"  {Fore.CYAN}[*] กำลังเปิด Digital Workbook: {course_name}{Style.RESET_ALL}")
        result = await self.page.evaluate(f'''
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
        ''', course_name)
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
                await self.page.wait_for_selector('.card, .unit-detail, [class*="unit"]', timeout=5000)
                break
            except Exception:
                await asyncio.sleep(2)

        units_data = []
        for retry in range(3):
            units_data = await self.page.evaluate('''
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
            ''')
            if units_data:
                break
            await asyncio.sleep(2)

        print(f"  {Fore.GREEN}[+] พบ {len(units_data)} บทเรียน{Style.RESET_ALL}")
        return units_data

    async def click_unit(self, unit_name):
        print(f"  {Fore.CYAN}[*] กำลังเปิดบท: {unit_name}{Style.RESET_ALL}")
        result = await self.page.evaluate(f'''
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
        ''', unit_name)
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
            await self.page.wait_for_selector('.activity-info, [class*="activity"], [class*="lesson"], .card', timeout=8000)
        except Exception:
            pass

        exercises_data = await self.page.evaluate(fr'''
            (unitName) => {{
                const result = {{ unit_name: unitName, lessons: [] }};

                let unitCard = null;
                const allCards = document.querySelectorAll('.card, [class*="unit"], [class*="product"]');
                for (const card of allCards) {{
                    const cardText = (card.innerText || '').toLowerCase();
                    if (cardText.includes(unitName.toLowerCase())) {{
                        unitCard = card;
                        break;
                    }}
                }}
                if (!unitCard) unitCard = document.body;

                let currentLesson = null;

                const invalidNames = ['completed', 'in progress', 'not started', 'in-progress', 'not-started'];

                const checkStatus = (el) => {{
                    const html = el.innerHTML || '';
                    if (html.includes('nemo-tick') || html.includes('green-tick') || html.includes('lch-green-tick') || html.includes('completed') || html.includes('svg')) {{
                        return 'completed';
                    }}
                    if (html.includes('inprogress') || html.includes('in-progress') || html.includes('progress')) {{
                        return 'in-progress';
                    }}
                    return 'pending';
                }};

                const allNodes = Array.from(unitCard.querySelectorAll('*'));

                for (const node of allNodes) {{
                    const text = (node.innerText || '').trim();
                    if (!text) continue;

                    const isLessonHeader = /^Lesson\s+[A-Z]/i.test(text) && text.length < 20;

                    if (isLessonHeader) {{
                        let existingLesson = result.lessons.find(l => l.name.toLowerCase() === text.toLowerCase());
                        if (!existingLesson) {{
                            currentLesson = {{ name: text, exercises: [] }};
                            result.lessons.push(currentLesson);
                        }} else {{
                            currentLesson = existingLesson;
                        }}
                        continue;
                    }}

                    const isActivity = node.matches('.activity-info, [class*="activity"], a[class*="activity"], button[class*="activity"]') ||
                                       (node.tagName === 'A' && node.querySelector('.star, [class*="star"], i, img')) ||
                                       (node.classList && Array.from(node.classList).some(c => c.includes('activity') || c.includes('item')));

                    if (isActivity) {{
                        const lines = text.split('\n').map(l => l.trim()).filter(l => l.length > 0);
                        const exName = lines[0];

                        if (!exName || /^Lesson\s+[A-Z]/i.test(exName) || exName.length < 2 ||
                            invalidNames.includes(exName.toLowerCase()) ||
                            exName.toLowerCase() === unitName.toLowerCase() ||
                            /^\d+\s+/.test(exName)) {{
                            continue;
                        }}

                        if (!currentLesson) {{
                            currentLesson = {{ name: 'Lesson A', exercises: [] }};
                            result.lessons.push(currentLesson);
                        }}

                        if (!currentLesson.exercises.some(e => e.name.toLowerCase() === exName.toLowerCase())) {{
                            const status = checkStatus(node);
                            currentLesson.exercises.push({{ name: exName, status: status }});
                        }}
                    }}
                }}

                if (result.lessons.length === 0 || result.lessons.every(l => l.exercises.length === 0)) {{
                    result.lessons = [];
                    const fallbackLesson = {{ name: 'Exercises', exercises: [] }};

                    const clickables = unitCard.querySelectorAll('a, button, [role="button"]');
                    clickables.forEach(item => {{
                        const rawText = (item.innerText || '').trim();
                        if (!rawText) return;

                        const lines = rawText.split('\n').map(l => l.trim()).filter(l => l.length > 0);
                        const name = lines[0];

                        if (name && name.length > 2 && 
                            !name.toLowerCase().includes('lesson') &&
                            !name.toLowerCase().includes('unit') && 
                            !invalidNames.includes(name.toLowerCase()) &&
                            name.toLowerCase() !== unitName.toLowerCase() && 
                            !/^\d+\s+/.test(name)) {{
                            if (!fallbackLesson.exercises.some(e => e.name === name)) {{
                                const status = checkStatus(item);
                                fallbackLesson.exercises.push({{ name: name, status: status }});
                            }}
                        }}
                    }});

                    if (fallbackLesson.exercises.length > 0) {{
                        result.lessons.push(fallbackLesson);
                    }}
                }}

                return result;
            }}
        ''', unit_name)

        print(f"  {Fore.GREEN}[+] พบ {len(exercises_data['lessons'])} Lesson{Style.RESET_ALL}")
        return exercises_data

    async def click_exercise(self, exercise_name):
        print(f"  {Fore.CYAN}[*] กำลังเปิดแบบฝึกหัด: {exercise_name}{Style.RESET_ALL}")

        current_url = self.page.url
        if '/view/' in current_url or '/activity/' in current_url or 'item' in current_url:
            current_title = await self.page.evaluate('''
                () => {
                    const el = document.querySelector('.activity-title, h1, header span, .product-title, .title');
                    return el ? el.innerText.trim().toLowerCase() : '';
                }
            ''')
            if current_title and exercise_name.lower() in current_title:
                print(f"  {Fore.GREEN}[+] อยู่ในหน้าแบบฝึกหัดข้อนี้เรียบร้อยแล้ว{Style.RESET_ALL}")
                await self.debug_wait(3)
                return True

            print(f"  {Fore.CYAN}[*] กำลังย้อนกลับสู่หน้า Workbook...{Style.RESET_ALL}")
            await self.page.go_back()
            await self.safe_wait_for_load()
            await self.debug_wait(1)

        result = await self.page.evaluate(fr'''
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
        ''', exercise_name)

        if result:
            print(f"  {Fore.GREEN}[+] เปิดแบบฝึกหัดสำเร็จ{Style.RESET_ALL}")
            await self.safe_wait_for_load()
            await self.wait_for_loading_spinner(timeout=10)
            await self.debug_wait(3)
            return True
        print(f"  {Fore.RED}[-] ไม่พบแบบฝึกหัดนี้{Style.RESET_ALL}")
        return False

    # ==================== Parsing ====================

    def _parse_js_object(self, content):
        print(f"  {Fore.CYAN}[PARSE] เริ่มแกะ data.js (ขนาด {len(content)} bytes)...{Style.RESET_ALL}")
        m = re.search(r'ajaxData\s*=\s*', content)
        if not m:
            print(f"  {Fore.RED}[PARSE] ไม่พบ 'ajaxData ='{Style.RESET_ALL}")
            return None

        start_idx = m.end()
        decoder = json.JSONDecoder()
        try:
            data, end_idx = decoder.raw_decode(content, start_idx)
            print(f"  {Fore.GREEN}[PARSE] แกะ JSON สำเร็จ — keys: {len(data)}{Style.RESET_ALL}")
            return data
        except json.JSONDecodeError as e:
            last_brace = content.rfind('}')
            if last_brace > start_idx:
                snippet = content[start_idx:last_brace+1]
                try:
                    data = json.loads(snippet)
                    print(f"  {Fore.GREEN}[PARSE] fallback json.loads สำเร็จ — keys: {len(data)}{Style.RESET_ALL}")
                    return data
                except Exception as e2:
                    print(f"  {Fore.RED}[PARSE] fallback ล้มเหลว: {e2}{Style.RESET_ALL}")
                    return None
        except Exception as e:
            print(f"  {Fore.RED}[PARSE] error: {e}{Style.RESET_ALL}")
            return None

    def _decode_unicode_escapes(self, text):
        res = text
        try:
            res = codecs.decode(text, 'unicode_escape')
        except Exception:
            try:
                res = html_module.unescape(codecs.decode(text, 'unicode_escape'))
            except Exception:
                try:
                    res = text.encode('latin-1', 'backslashreplace').decode('unicode_escape')
                except Exception:
                    res = text
        res = self._normalize_text(res)
        return res

    def _normalize_text(self, text):
        if not text:
            return ''

        text = text.replace('â€™', "'").replace('â€œ', '"').replace('â€', '"')
        text = text.replace('â€“', '-').replace('â€˜', "'").replace('â', "'")

        replacements = {
            '\u2019': "'",
            '\u2018': "'",
            '\u201c': '"',
            '\u201d': '"',
            '\u2013': '-',
            '\u2014': '-',
            '\u2026': '...',
            '\u00a0': ' ',
            '\u200b': '',
            '`': "'",
        }
        for k, v in replacements.items():
            text = text.replace(k, v)
            
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _extract_answers_from_xml(self, xml_content):
        try:
            root = ET.fromstring(xml_content)
        except Exception as e:
            print(f"  {Fore.RED}[XML] parse error: {e}{Style.RESET_ALL}")
            return []

        qti = '{http://www.imsglobal.org/xsd/imsqti_v2p1}'

        instruction = self._extract_rubric_text(root, qti)
        audio_filename = self._extract_audio_filename(root, qti)

        questions = []

        mc_interactions = root.findall(f'.//{qti}choiceInteraction')
        if not mc_interactions:
            mc_interactions = root.findall('.//choiceInteraction')
        if mc_interactions:
            questions.extend(self._parse_multiple_choice(
                root, qti, mc_interactions, instruction, audio_filename
            ))
            print(f"  {Fore.CYAN}[XML] Multiple Choice: {len(mc_interactions)} interaction(s){Style.RESET_ALL}")

        dd_interactions = root.findall(f'.//{qti}inlineChoiceInteraction')
        if not dd_interactions:
            dd_interactions = root.findall('.//inlineChoiceInteraction')
        if dd_interactions:
            questions.extend(self._parse_dropdown(
                root, qti, dd_interactions, instruction, audio_filename
            ))
            print(f"  {Fore.CYAN}[XML] Dropdown: {len(dd_interactions)} interaction(s){Style.RESET_ALL}")

        te_interactions = root.findall(f'.//{qti}textEntryInteraction')
        if not te_interactions:
            te_interactions = root.findall('.//textEntryInteraction')
        if te_interactions:
            questions.extend(self._parse_text_entry(
                root, qti, te_interactions, instruction, audio_filename
            ))
            print(f"  {Fore.CYAN}[XML] Text Entry: {len(te_interactions)} interaction(s){Style.RESET_ALL}")

        gm_interactions = root.findall(f'.//{qti}gapMatchInteraction')
        if not gm_interactions:
            gm_interactions = root.findall('.//gapMatchInteraction')
        if gm_interactions:
            questions.extend(self._parse_gap_match(
                root, qti, gm_interactions, instruction, audio_filename
            ))
            print(f"  {Fore.CYAN}[XML] Gap Match: {len(gm_interactions)} interaction(s){Style.RESET_ALL}")

        return questions

    def _extract_rubric_text(self, root, qti):
        rubric = root.find(f'.//{qti}div[@id="rubric"]')
        if rubric is None:
            rubric = root.find('.//div[@id="rubric"]')
        if rubric is None:
            return ''
        for p in rubric.findall(f'.//{qti}p'):
            text = ''.join(p.itertext()).strip()
            if text:
                return text
        for p in rubric.findall('.//p'):
            text = ''.join(p.itertext()).strip()
            if text:
                return text
        return ''.join(rubric.itertext()).strip()

    def _extract_audio_filename(self, root, qti):
        header_audio = root.find(f'.//{qti}div[@id="headerAudio"]')
        if header_audio is None:
            header_audio = root.find('.//div[@id="headerAudio"]')
        if header_audio is None:
            return ''
        data_author = header_audio.get('data-author', '') or ''
        try:
            data_author_clean = html_module.unescape(data_author)
            audio_info = json.loads(data_author_clean)
            return audio_info.get('mediaID', '')
        except Exception:
            m = re.search(r'"mediaID"\s*:\s*"([^"]+)"', data_author)
            return m.group(1) if m else ''

    def _parse_multiple_choice(self, root, qti, interactions, instruction, audio_filename):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            answers = []
            correct_texts = []
            correct_ids = []

            choices = interaction.findall(f'.//{qti}simpleChoice')
            if not choices:
                choices = interaction.findall('.//simpleChoice')

            response_id = (interaction.get('responseIdentifier') or '').strip()
            
            response_decl = None
            for rd in root.findall(f'.//{qti}responseDeclaration'):
                if (rd.get('identifier') or '').strip() == response_id:
                    response_decl = rd
                    break
            
            if response_decl is None:
                response_decl = root.find(f'.//{qti}responseDeclaration')
            if response_decl is None:
                response_decl = root.find('.//responseDeclaration')

            if response_decl is not None:
                cr = response_decl.find(f'.//{qti}correctResponse')
                if cr is None:
                    cr = response_decl.find('.//correctResponse')
                if cr is not None:
                    for v in cr.findall(f'.//{qti}value'):
                        if v.text:
                            correct_ids.append(v.text.strip())
                    if not correct_ids:
                        for v in cr.findall('.//value'):
                            if v.text:
                                correct_ids.append(v.text.strip())

            if not correct_ids:
                for choice in choices:
                    feedback = choice.get('answerfeedback', '') or ''
                    if '#feedback:p1#' in feedback:
                        cid = (choice.get('identifier') or '').strip()
                        if cid:
                            correct_ids.append(cid)

            for choice in choices:
                choice_text = self._normalize_text(''.join(choice.itertext()))
                if not choice_text:
                    continue
                answers.append(choice_text)
                
                choice_id = (choice.get('identifier') or '').strip()
                if choice_id in correct_ids:
                    correct_texts.append(choice_text)

            if not correct_texts:
                for choice in choices:
                    feedback = choice.get('answerfeedback', '') or ''
                    if '#feedback:p1#' in feedback:
                        choice_text = self._normalize_text(''.join(choice.itertext()))
                        if choice_text:
                            correct_texts.append(choice_text)

            if answers:
                questions.append({
                    'type': 'Multiple Choice',
                    'question_number': i,
                    'question': instruction or f'Question {i}',
                    'instruction': instruction,
                    'audio_filename': audio_filename,
                    'answers': answers,
                    'correct_answer': correct_texts[0] if correct_texts else '',
                    'correct_answers': correct_texts,
                    'is_multiple_answers': len(correct_texts) > 1,
                    'correct_id': correct_ids[0] if correct_ids else '',
                    'response_id': response_id
                })
        return questions

    def _parse_dropdown(self, root, qti, interactions, instruction, audio_filename):
        questions = []

        response_map = {}
        for rd in root.findall(f'.//{qti}responseDeclaration'):
            rid = (rd.get('identifier') or '').strip()
            if not rid:
                continue
            cr = rd.find(f'.//{qti}correctResponse')
            if cr is None:
                cr = rd.find('.//correctResponse')
            if cr is not None:
                val = cr.find(f'.//{qti}value')
                if val is None:
                    val = cr.find('.//value')
                if val is not None and val.text:
                    response_map[rid] = val.text.strip()

        print(f"  {Fore.CYAN}[DROPDOWN] พบ responseDeclaration {len(response_map)} ตัว{Style.RESET_ALL}")

        fallback_answers = []
        for p in root.findall(f'.//{qti}p'):
            txt = ''.join(p.itertext()).strip()
            if 'Correct Answer:' in txt or 'Correct answer:' in txt:
                parts = txt.split('Correct Answer:')
                if len(parts) < 2:
                    parts = txt.split('Correct answer:')
                if len(parts) >= 2:
                    ans = self._normalize_text(parts[1].strip())
                    if ans:
                        fallback_answers.append(ans)

        contentblock = root.find(f'.//{qti}div[@id="contentblock"]')
        if contentblock is None:
            contentblock = root.find('.//div[@id="contentblock"]')

        for i, interaction in enumerate(interactions, 1):
            response_id = (interaction.get('responseIdentifier') or '').strip()

            options = []
            for choice in interaction.findall(f'.//{qti}inlineChoice'):
                text = self._normalize_text(''.join(choice.itertext()))
                cid = (choice.get('identifier') or '').strip()
                if text:
                    options.append((cid, text))
            if not options:
                for choice in interaction.findall('.//inlineChoice'):
                    text = self._normalize_text(''.join(choice.itertext()))
                    cid = (choice.get('identifier') or '').strip()
                    if text:
                        options.append((cid, text))

            correct_id = response_map.get(response_id, '')
            correct_answer = ''
            for cid, text in options:
                if cid == correct_id:
                    correct_answer = text
                    break

            if not correct_answer:
                for choice in interaction.findall(f'.//{qti}inlineChoice'):
                    feedback = choice.get('answerfeedback', '') or ''
                    if '#feedback:p1#' in feedback:
                        correct_answer = self._normalize_text(''.join(choice.itertext()))
                        correct_id = (choice.get('identifier') or '').strip()
                        break
                if not correct_answer:
                    for choice in interaction.findall('.//inlineChoice'):
                        feedback = choice.get('answerfeedback', '') or ''
                        if '#feedback:p1#' in feedback:
                            correct_answer = self._normalize_text(''.join(choice.itertext()))
                            correct_id = (choice.get('identifier') or '').strip()
                            break

            if not correct_answer and i <= len(fallback_answers):
                correct_answer = fallback_answers[i - 1]

            context = self._get_dropdown_context_clean(contentblock, qti, interaction)

            questions.append({
                'type': 'Dropdown',
                'question_number': i,
                'question': context or instruction or f'Dropdown {i}',
                'instruction': instruction,
                'audio_filename': audio_filename,
                'answers': [t for _, t in options],
                'correct_answer': self._normalize_text(correct_answer),
                'correct_id': correct_id,
                'response_id': response_id
            })

        filled = sum(1 for q in questions if q['correct_answer'])
        print(f"  {Fore.CYAN}[DROPDOWN] parse ได้ {len(questions)} ข้อ, มีคำตอบ {filled} ข้อ{Style.RESET_ALL}")
        return questions

    def _get_dropdown_context_clean(self, contentblock, qti, interaction):
        if contentblock is None:
            return ''
        try:
            for p in contentblock.findall(f'.//{qti}p'):
                for sub in p.iter():
                    if sub is interaction:
                        parts = []
                        for child in p.iter():
                            if child is interaction:
                                parts.append('[...]')
                            if child.text:
                                parts.append(child.text)
                            if child.tail:
                                parts.append(child.tail)
                        text = ''.join(parts)
                        text = re.sub(r'\s+', ' ', text).strip()
                        return text[:300]

            for p in contentblock.findall('.//p'):
                for sub in p.iter():
                    if sub is interaction:
                        parts = []
                        for child in p.iter():
                            if child is interaction:
                                parts.append('[...]')
                            if child.text:
                                parts.append(child.text)
                            if child.tail:
                                parts.append(child.tail)
                        text = ''.join(parts)
                        text = re.sub(r'\s+', ' ', text).strip()
                        return text[:300]
        except Exception:
            pass
        return ''

    def _parse_text_entry(self, root, qti, interactions, instruction, audio_filename):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            response_id = (interaction.get('responseIdentifier') or '').strip()

            accepted_answers = []
            if response_id:
                for rd in root.findall(f'.//{qti}responseDeclaration'):
                    if (rd.get('identifier') or '').strip() == response_id:
                        cr = rd.find(f'.//{qti}correctResponse')
                        if cr is None:
                            cr = rd.find('.//correctResponse')
                        if cr is not None:
                            for v in cr.findall(f'.//{qti}value'):
                                if v.text:
                                    accepted_answers.append(self._normalize_text(v.text))
                            if not accepted_answers:
                                for v in cr.findall('.//value'):
                                    if v.text:
                                        accepted_answers.append(self._normalize_text(v.text))
                        break

            contentblock = root.find(f'.//{qti}div[@id="contentblock"]')
            if contentblock is None:
                contentblock = root.find('.//div[@id="contentblock"]')
            context = self._get_dropdown_context_clean(contentblock, qti, interaction)

            questions.append({
                'type': 'Text Entry',
                'question_number': i,
                'question': context or instruction or f'Gap {i}',
                'instruction': instruction,
                'audio_filename': audio_filename,
                'answers': accepted_answers,
                'correct_answer': accepted_answers[0] if accepted_answers else '',
                'response_id': response_id
            })
        return questions

    def _parse_gap_match(self, root, qti, interactions, instruction, audio_filename):
        questions = []
        for i, interaction in enumerate(interactions, 1):
            gap_texts = {}
            for gt in interaction.findall(f'.//{qti}gapText'):
                gt_id = (gt.get('identifier') or '').strip()
                gt_text = self._normalize_text(''.join(gt.itertext()))
                if gt_id and gt_text:
                    gap_texts[gt_id] = gt_text
            if not gap_texts:
                for gt in interaction.findall('.//gapText'):
                    gt_id = (gt.get('identifier') or '').strip()
                    gt_text = self._normalize_text(''.join(gt.itertext()))
                    if gt_id and gt_text:
                        gap_texts[gt_id] = gt_text

            correct_pairs = []
            for rd in root.findall(f'.//{qti}responseDeclaration'):
                cr = rd.find(f'.//{qti}correctResponse')
                if cr is None:
                    cr = rd.find('.//correctResponse')
                if cr is not None:
                    for v in cr.findall(f'.//{qti}value'):
                        if v.text:
                            correct_pairs.append(v.text.strip())
                    if not correct_pairs:
                        for v in cr.findall('.//value'):
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
                for p in contentblock.findall(f'.//{qti}p'):
                    text = ''.join(p.itertext()).strip()
                    if not text or text.startswith('#') or text.startswith('Correct!') or text.startswith('Incorrect!'):
                        continue
                    text = re.sub(r'\s+', ' ', text)
                    context_parts.append(text)
                if not context_parts:
                    for p in contentblock.findall('.//p'):
                        text = ''.join(p.itertext()).strip()
                        if not text or text.startswith('#') or text.startswith('Correct!') or text.startswith('Incorrect!'):
                            continue
                        text = re.sub(r'\s+', ' ', text)
                        context_parts.append(text)

            all_options = list(gap_texts.values())

            questions.append({
                'type': 'Drag & Drop',
                'question_number': i,
                'question': ' | '.join(context_parts) if context_parts else instruction,
                'instruction': instruction,
                'audio_filename': audio_filename,
                'answers': all_options,
                'correct_answer': '',
                'correct_pairs': id_to_text,
                'gap_texts': gap_texts
            })
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
                print(f"  {Fore.GREEN}[+] ใช้ content ที่ intercept ได้ ({len(content)} bytes){Style.RESET_ALL}")
            else:
                try:
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()
                    content = response.text
                    print(f"  {Fore.GREEN}[+] ดาวน์โหลดสำเร็จ ({len(content)} bytes){Style.RESET_ALL}")
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
                if not filename.endswith('.xml'):
                    continue
                xml_count += 1
                xml_content = data[filename]

                if 'You have finished the activity.' in str(xml_content):
                    continue

                decoded_xml = self._decode_unicode_escapes(xml_content)
                questions = self._extract_answers_from_xml(decoded_xml)
                if questions:
                    all_answers[filename] = questions
                    total_questions += len(questions)
                    print(f"      {Fore.GREEN}[+] {filename}: ได้ {len(questions)} ข้อ{Style.RESET_ALL}")

            print(f"  {Fore.CYAN}[PARSE] รวม {xml_count} XML, ได้ {total_questions} คำถาม{Style.RESET_ALL}")

            if all_answers:
                break

        if not all_answers:
            print(f"\n  {Fore.RED}[-] ไม่พบเฉลยจาก data.js{Style.RESET_ALL}")
            return None

        return all_answers

    def _print_exercise_answers_by_file(self, exercise_name, answers_by_file):
        """แสดงเฉลยที่แยกตามไฟล์ XML ใน data.js"""
        print(f"\n  {Fore.CYAN}{exercise_name}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

        if not answers_by_file:
            print(f"  {Fore.YELLOW}[!] ไม่พบเฉลย{Style.RESET_ALL}")
            return

        question_number = 0
        for filename, questions in answers_by_file.items():
            print(f"\n  {Fore.MAGENTA}[{filename}]{Style.RESET_ALL}")
            for question in questions:
                question_number += 1
                question_type = question.get('type', 'Unknown')
                prompt = question.get('question') or question.get('instruction') or '-'
                print(f"\n      [{question_number}] {question_type}")
                print(f"      คำถาม: {prompt}")

                if question_type == 'Drag & Drop':
                    pairs = question.get('correct_pairs', {})
                    if pairs:
                        print("      คำตอบ:")
                        for gap_id, answer in pairs.items():
                            print(f"        {gap_id}: {answer}")
                    continue

                correct_answers = question.get('correct_answers') or []
                correct_answer = question.get('correct_answer', '')
                if correct_answers:
                    answer_text = ', '.join(correct_answers)
                else:
                    answer_text = correct_answer or '-'
                print(f"      คำตอบ: {Fore.GREEN}{answer_text}{Style.RESET_ALL}")

        print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

    async def _get_answers_from_page(self, exercise_name, go_back_after=False, already_open=False):
        if not already_open:
            if not await self.click_exercise(exercise_name):
                return None

        await self.safe_wait_for_load()
        await self.debug_wait(0.5)

        answers = await self.page.evaluate("""
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
        """)

        print(f"\n  {Fore.CYAN}{exercise_name}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        if answers.get('question'):
            print(f"\n      คําถาม: {answers['question']}\n")

        if answers.get('options'):
            print(f"      ตัวเลือก:")
            for i, option in enumerate(answers['options'], 1):
                if option.get('is_correct'):
                    print(f"        {Fore.GREEN}{i}. {option['text']}   <-- คําตอบ{Style.RESET_ALL}")
                else:
                    print(f"        {Fore.WHITE}{i}. {option['text']}{Style.RESET_ALL}")

        if answers.get('correct_answers'):
            print(f"      {Fore.GREEN}คําตอบ: {', '.join(answers['correct_answers'])}{Style.RESET_ALL}")

        print(f"  {Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")

        if go_back_after:
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับไปเลือกรายการ...{Style.RESET_ALL}")
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


# ==================== Display / Menu ====================

def display_courses(courses):
    print(f"\n  {'=' * 70}")
    print(f"  {Fore.CYAN}คอร์สที่มี Digital Workbook{Style.RESET_ALL}")
    print(f"  {'=' * 70}\n")
    if not courses:
        print(f"  {Fore.YELLOW}[!] ไม่พบคอร์ส{Style.RESET_ALL}")
        return
    for i, course in enumerate(courses, 1):
        print(f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {Fore.CYAN}{course['courseName']}{Style.RESET_ALL}")
        print(f"      {Fore.GREEN}{course['workbookName']}{Style.RESET_ALL}\n")


def display_units(units):
    print(f"\n  {'=' * 70}")
    print(f"  {Fore.CYAN}เลือกบทที่ต้องการดู{Style.RESET_ALL}")
    print(f"  {'=' * 70}\n")
    if not units:
        print(f"  {Fore.YELLOW}[!] ไม่พบบทเรียน{Style.RESET_ALL}")
        return
    for i, unit in enumerate(units, 1):
        print(f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {Fore.CYAN}{unit['name']}{Style.RESET_ALL}")


def display_exercises(data):
    print(f"\n  {'=' * 70}")
    print(f"  {Fore.CYAN}{data['unit_name']}{Style.RESET_ALL}")
    print(f"  {'=' * 70}\n")
    if not data.get('lessons'):
        print(f"  {Fore.YELLOW}[!] ไม่มีแบบฝึกหัด{Style.RESET_ALL}")
        return
    exercise_counter = 1
    for lesson in data['lessons']:
        print(f"  {Fore.MAGENTA}+-- {lesson['name']}{Style.RESET_ALL}")
        for ex in lesson['exercises']:
            if ex['status'] == 'completed':
                print(f"  {Fore.GREEN}|   [{exercise_counter}] [OK] {ex['name']}{Style.RESET_ALL}")
            elif ex['status'] == 'in-progress':
                print(f"  {Fore.YELLOW}|   [{exercise_counter}] [..] {ex['name']}{Style.RESET_ALL}")
            else:
                print(f"  {Fore.WHITE}|   [{exercise_counter}] [  ] {ex['name']}{Style.RESET_ALL}")
            exercise_counter += 1
        print(f"  {Fore.MAGENTA}+--{Style.RESET_ALL}\n")


def choose_account(scraper):
    accounts = scraper.load_accounts()
    print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}บัญชี Cambridge One / Chrome profiles{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

    if accounts:
        for i, account in enumerate(accounts, 1):
            has_pwd = ' [saved password]' if account.get('password') else ''
            print(f"  [{i}] {account.get('email', '')}{has_pwd}")
        print("  [N] เพิ่มบัญชีใหม่")
        print("  [D] ลบรหัสผ่านที่บันทึกไว้")
        print("  [0] ออกจากโปรแกรม")
        try:
            choice = input(f"\n  {Fore.CYAN}[>] เลือกบัญชี: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            choice = '1'

        if choice == '0':
            return None, None, False
        if choice.lower() == 'n':
            try:
                email = input(f"\n  {Fore.CYAN}[>] Email บัญชีใหม่: {Style.RESET_ALL}").strip()
            except Exception:
                return None, None, False
            if not email:
                return None, None, False
            return email, None, True
        if choice.lower() == 'd':
            try:
                email = input(f"\n  {Fore.CYAN}[>] Email ที่ต้องการลบรหัสผ่าน: {Style.RESET_ALL}").strip()
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
                return acc.get('email', ''), acc.get('password'), True
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


async def auto_process_single_unit(scraper, selected_course, selected_unit, exercises_data, do_all=False):
    all_exercises = []
    for lesson in exercises_data.get('lessons', []):
        for ex in lesson['exercises']:
            all_exercises.append({'lesson': lesson['name'], 'name': ex['name'], 'status': ex['status']})

    if do_all:
        exercises_to_do = all_exercises
        print(f"  {Fore.CYAN}[*] โหมดทำทุกข้อ: {len(exercises_to_do)} ข้อ{Style.RESET_ALL}")
    else:
        exercises_to_do = [ex for ex in all_exercises if ex['status'] != 'completed']
        print(f"  {Fore.CYAN}[*] โหมดทำเฉพาะข้อที่ยังไม่ทำ: {len(exercises_to_do)} ข้อ{Style.RESET_ALL}")

    if not exercises_to_do:
        print(f"  {Fore.GREEN}[+] ไม่มีข้อที่ต้องทำในบทนี้!{Style.RESET_ALL}")
        return [], []

    total_count = len(exercises_to_do)
    summary = []
    skipped = []

    for i, ex in enumerate(exercises_to_do, 1):
        print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[{i}/{total_count}] กำลังทำ: {ex['name']}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

        scraper.reset_data_js_tracking()

        if not await scraper.click_exercise(ex['name']):
            summary.append({'name': ex['name'], 'status': 'FAILED', 'reason': 'เปิดแบบฝึกหัดไม่ได้'})
            continue

        try:
            is_video_tf = await scraper.check_video_true_false()
        except Exception:
            is_video_tf = False

        if is_video_tf:
            has_data = await scraper._wait_for_data_js(timeout=10)
            if not has_data:
                skipped.append({'name': ex['name'], 'reason': 'ไม่พบ data.js'})
                try:
                    await scraper.page.go_back()
                    await scraper.safe_wait_for_load()
                except Exception:
                    pass
                continue

        cached = scraper.db_get(selected_course['courseName'], selected_unit['name'], ex['name'])
        if cached:
            print(f"  {Fore.CYAN}[*] ใช้ข้อมูลจากฐานข้อมูล{Style.RESET_ALL}")
            answers = cached
        else:
            await scraper._wait_for_data_js(timeout=20)
            answers = await scraper.get_answers_from_data_js()
            if answers:
                scraper.db_set(selected_course['courseName'], selected_unit['name'], ex['name'], answers)
            else:
                answers = None

        fill_result = await scraper.auto_fill_and_check(ex['name'], answers)

        if not fill_result['success']:
            summary.append({
                'name': ex['name'], 'status': 'FAILED',
                'reason': fill_result['reason'],
                'score': fill_result.get('score')
            })
            print(f"  {Fore.RED}[!] ข้อ '{ex['name']}' ทำไม่สำเร็จ: {fill_result['reason']}{Style.RESET_ALL}")
            try:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
                await scraper.debug_wait(1)
            except Exception:
                pass
            continue

        await scraper.check_and_retry_if_needed(ex['name'], answers)

        is_multi_page = await scraper.is_multi_question_mc()
        if not is_multi_page:
            await scraper.submit_single_page_activity()

        summary.append({
            'name': ex['name'], 'status': 'OK', 'score': fill_result.get('score')
        })

        await scraper.debug_wait(2)

    return summary, skipped


async def run_all_units_auto(scraper, selected_course, do_all=False):
    print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}🚀 โหมดทำทุกบท ทุกงาน{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

    units = await scraper.get_all_units()
    if not units:
        return

    print(f"  {Fore.GREEN}[+] พบ {len(units)} บทเรียน{Style.RESET_ALL}")

    all_summary = []
    all_skipped = []

    for unit_idx, unit in enumerate(units, 1):
        print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}[Unit {unit_idx}/{len(units)}] กำลังทำบท: {unit['name']}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")

        try:
            current_url = scraper.page.url
            if '/view/' in current_url or '/activity/' in current_url:
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()
                await scraper.debug_wait(2)
        except Exception:
            pass

        if not await scraper.click_unit(unit['name']):
            continue

        exercises_data = await scraper.get_exercises_in_unit(unit['name'])
        summary, skipped = await auto_process_single_unit(scraper, selected_course, unit, exercises_data, do_all=do_all)
        all_summary.extend(summary)
        all_skipped.extend(skipped)

        try:
            await scraper.page.go_back()
            await scraper.safe_wait_for_load()
            await scraper.debug_wait(2)
        except Exception:
            pass

    print(f"\n  {Fore.CYAN}📊 สรุปผลการทำงานทั้งหมด{Style.RESET_ALL}")

    ok_count = sum(1 for s in all_summary if s['status'] == 'OK')
    fail_count = sum(1 for s in all_summary if s['status'] == 'FAILED')

    for i, s in enumerate(all_summary, 1):
        if s['status'] == 'OK':
            score_str = f" (Score: {s['score']})" if s.get('score') else ""
            print(f"  [{i}] {Fore.GREEN}✓ {s['name']}{score_str}{Style.RESET_ALL}")
        else:
            print(f"  [{i}] {Fore.RED}✗ {s['name']} — {s.get('reason', '')}{Style.RESET_ALL}")

    print(f"\n  {Fore.GREEN}สำเร็จ: {ok_count}{Style.RESET_ALL}")
    if fail_count > 0:
        print(f"  {Fore.RED}ล้มเหลว: {fail_count}{Style.RESET_ALL}")


async def run_unit_menu(scraper, selected_course, selected_unit, exercises_data):
    while True:
        display_exercises(exercises_data)

        all_exercises = []
        for lesson in exercises_data.get('lessons', []):
            for ex in lesson['exercises']:
                all_exercises.append({'lesson': lesson['name'], 'name': ex['name'], 'status': ex['status']})

        print(f"\n  {Fore.CYAN}เลือกสิ่งที่ต้องการทำ:{Style.RESET_ALL}")
        print(f"  [1] แสดงเฉลยทั้งหมดในบทนี้")
        print(f"  [2] ทำแบบฝึกหัดอัตโนมัติทั้งหมด")
        print(f"  [3] เลือกแบบฝึกหัดเฉพาะข้อ")
        print(f"  [4] ดูฐานข้อมูลคำตอบที่เคยบันทึก")
        print(f"  [r] Refresh ข้อมูลบทนี้")
        print(f"  [0] กลับไปเลือกบทใหม่")

        try:
            action_choice = input(f"\n  {Fore.CYAN}[>] เลือก (0-4, r): {Style.RESET_ALL}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            action_choice = '0'

        if action_choice == '1':
            exercises_to_show = [ex for ex in all_exercises if ex['status'] != 'completed']
            if not exercises_to_show:
                print(f"  {Fore.GREEN}[+] ทุกข้อทำเสร็จแล้ว!{Style.RESET_ALL}")
            else:
                for i, ex in enumerate(exercises_to_show, 1):
                    print(f"\n  {Fore.CYAN}[{i}/{len(exercises_to_show)}] {ex['name']}{Style.RESET_ALL}")
                    scraper.reset_data_js_tracking()
                    if not await scraper.click_exercise(ex['name']):
                        continue
                    cached = scraper.db_get(selected_course['courseName'], selected_unit['name'], ex['name'])
                    if cached:
                        scraper._print_exercise_answers_by_file(ex['name'], cached)
                    else:
                        await scraper._wait_for_data_js(timeout=15)
                        answers = await scraper.get_answers_from_data_js()
                        if answers:
                            scraper._print_exercise_answers_by_file(ex['name'], answers)
                            scraper.db_set(selected_course['courseName'], selected_unit['name'], ex['name'], answers)
                    await scraper.page.go_back()
                    await scraper.safe_wait_for_load()

            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass

        elif action_choice == '2':
            print(f"\n  {Fore.CYAN}เลือกขอบเขต:{Style.RESET_ALL}")
            print(f"  [1] ทำเฉพาะข้อที่ยังไม่ทำ")
            print(f"  [2] ทำทุกข้อ")
            print(f"  [0] ยกเลิก")
            try:
                scope_choice = input(f"\n  {Fore.CYAN}[>] เลือก (0-2): {Style.RESET_ALL}").strip()
            except Exception:
                scope_choice = '0'

            if scope_choice == '0':
                continue

            do_all = (scope_choice == '2')

            print(f"\n  {Fore.CYAN}เลือกโหมด:{Style.RESET_ALL}")
            print(f"  [1] ทำทุกงานในบทนี้")
            print(f"  [2] ทำทุกงานในทุกบท")
            print(f"  [0] ยกเลิก")
            try:
                mode_choice = input(f"\n  {Fore.CYAN}[>] เลือก (0-2): {Style.RESET_ALL}").strip()
            except Exception:
                mode_choice = '0'

            if mode_choice == '0':
                continue

            if mode_choice == '1':
                summary, skipped = await auto_process_single_unit(scraper, selected_course, selected_unit, exercises_data, do_all=do_all)
                ok_count = sum(1 for s in summary if s['status'] == 'OK')
                for i, s in enumerate(summary, 1):
                    if s['status'] == 'OK':
                        score_str = f" (Score: {s['score']})" if s.get('score') else ""
                        print(f"  [{i}] {Fore.GREEN}✓ {s['name']}{score_str}{Style.RESET_ALL}")
                    else:
                        print(f"  [{i}] {Fore.RED}✗ {s['name']} — {s.get('reason', '')}{Style.RESET_ALL}")
                print(f"\n  {Fore.GREEN}สำเร็จ: {ok_count}{Style.RESET_ALL}")

                try:
                    await scraper.page.go_back()
                    await scraper.safe_wait_for_load()
                except Exception:
                    pass

            elif mode_choice == '2':
                await run_all_units_auto(scraper, selected_course, do_all=do_all)

            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter เพื่อกลับ...{Style.RESET_ALL}")
            except Exception:
                pass

        elif action_choice == '3':
            print(f"\n  {Fore.CYAN}เลือกแบบฝึกหัด:{Style.RESET_ALL}")
            for i, ex in enumerate(all_exercises, 1):
                status_icon = '[OK]' if ex['status'] == 'completed' else '[..]' if ex['status'] == 'in-progress' else '[  ]'
                print(f"  [{i}] {status_icon} {ex['name']}")

            try:
                ex_choice = input(f"\n  {Fore.CYAN}[>] เลือก (ตัวเลข): {Style.RESET_ALL}").strip()
            except Exception:
                ex_choice = ""

            try:
                ex_idx = int(ex_choice) - 1
                if 0 <= ex_idx < len(all_exercises):
                    selected_ex = all_exercises[ex_idx]
                    await run_exercise_menu(scraper, selected_course, selected_unit, selected_ex)
            except ValueError:
                print(f"  {Fore.RED}[-] Please enter a valid number{Style.RESET_ALL}")

        elif action_choice == '4':
            print(f"\n  {Fore.CYAN}[*] ฐานข้อมูลคำตอบ:{Style.RESET_ALL}")
            if not scraper.answers_db:
                print(f"  {Fore.YELLOW}[!] ยังไม่มีข้อมูล{Style.RESET_ALL}")
            else:
                for key in sorted(scraper.answers_db.keys()):
                    print(f"  - {key}")
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
            except Exception:
                pass

        elif action_choice == 'r':
            exercises_data = await scraper.get_exercises_in_unit(selected_unit['name'])
            print(f"  {Fore.GREEN}[+] Refresh เสร็จ{Style.RESET_ALL}")
            await scraper.debug_wait(1)

        elif action_choice == '0':
            return

        else:
            print(f"  {Fore.RED}[-] Invalid option{Style.RESET_ALL}")
            await scraper.debug_wait(1)


async def run_exercise_menu(scraper, selected_course, selected_unit, selected_ex):
    while True:
        print(f"\n  {Fore.CYAN}เลือกการกระทำสำหรับ '{selected_ex['name']}':{Style.RESET_ALL}")
        print(f"  [1] แสดงเฉลย")
        print(f"  [2] เติมคำตอบ + กด Check + ส่งงาน")
        print(f"  [3] เติมคำตอบ + กด Check + Retry + ส่งงาน")
        print(f"  [4] ทำแบบฝึกหัดทักษะการพูด")
        print(f"  [5] Retry")
        print(f"  [6] เลือกทำข้ออื่น")
        print(f"  [0] กลับเมนูก่อนหน้า")

        try:
            sub_choice = input(f"\n  {Fore.CYAN}[>] เลือก (0-6): {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            sub_choice = '0'

        if sub_choice in ('1', '2', '3', '4'):
            cached = scraper.db_get(selected_course['courseName'], selected_unit['name'], selected_ex['name'])

            if cached:
                print(f"  {Fore.CYAN}[*] ใช้ข้อมูลจากฐานข้อมูล{Style.RESET_ALL}")
                answers = cached
                if not await scraper.click_exercise(selected_ex['name']):
                    try:
                        input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
                    except Exception:
                        pass
                    continue
            else:
                scraper.reset_data_js_tracking()
                if not await scraper.click_exercise(selected_ex['name']):
                    try:
                        input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
                    except Exception:
                        pass
                    continue

                await scraper._wait_for_data_js(timeout=15)
                answers = await scraper.get_answers_from_data_js()
                if answers:
                    scraper.db_set(selected_course['courseName'], selected_unit['name'], selected_ex['name'], answers)

            if answers or sub_choice in ('2', '3', '4'):
                if answers and sub_choice != '4':
                    scraper._print_exercise_answers_by_file(selected_ex['name'], answers)

                fill_result = None
                if sub_choice in ('2', '3'):
                    fill_result = await scraper.auto_fill_and_check(selected_ex['name'], answers)
                elif sub_choice == '4':
                    is_speaking = await scraper.handle_speaking_activity()
                    if is_speaking:
                        success = await scraper._click_check_button()
                        fill_result = {'success': success, 'reason': 'Speaking Activity Completed'}
                    else:
                        fill_result = {'success': False, 'reason': 'ไม่พบแบบฝึกหัดแบบพูด'}

                if fill_result and not fill_result.get('success'):
                    print(f"\n  {Fore.RED}⚠ ทำไม่สำเร็จ: {fill_result['reason']}{Style.RESET_ALL}")
                    print(f"\n  [1] ลองใหม่")
                    print(f"  [2] กลับ")
                    print(f"  [0] ปิดโปรแกรม")
                    try:
                        cc = input(f"\n  {Fore.CYAN}[>] เลือก (0-2): {Style.RESET_ALL}").strip()
                    except Exception:
                        cc = '2'

                    if cc == '1':
                        continue
                    elif cc == '0':
                        return
                    else:
                        return

                if sub_choice == '3':
                    await scraper.check_and_retry_if_needed(selected_ex['name'], answers)

                if sub_choice in ('2', '3', '4'):
                    res = await scraper.submit_activity_and_handle_result()

                    if isinstance(res, tuple):
                        action, new_name = res
                    else:
                        action, new_name = res, None

                    if action == 'close':
                        return
                    if action == 'back':
                        return
                    elif action == 'next':
                        current_url = scraper.page.url
                        if '/view/' not in current_url and '/activity/' not in current_url:
                            return
                        if new_name:
                            if re.match(r'^\d{9,}$', new_name.strip()):
                                return
                            selected_ex['name'] = new_name
                        continue
                    elif action == 'retry':
                        continue
            else:
                print(f"  {Fore.YELLOW}[!] ไม่พบเฉลยสำหรับข้อนี้{Style.RESET_ALL}")
                try:
                    input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
                except Exception:
                    pass
                return

            if sub_choice == '1':
                try:
                    input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
                except Exception:
                    pass
                await scraper.page.go_back()
                await scraper.safe_wait_for_load()

        elif sub_choice == '5':
            retried = await scraper.retry_exercise(selected_ex['name'])
            if not retried:
                await scraper.reset_exercise(selected_ex['name'])
            try:
                input(f"\n  {Fore.CYAN}[>] กด Enter...{Style.RESET_ALL}")
            except Exception:
                pass

        elif sub_choice in ('6', '0'):
            return

        else:
            print(f"  {Fore.RED}[-] Invalid option{Style.RESET_ALL}")
            await scraper.debug_wait(1)


async def main_async():
    print(f"""
    {Fore.GREEN}
     a88888b.  888888ba   .88888.     d888888P                   dP 
    d8'   `88  88    `8b d8'   `8b       88                      88 
    88        a88aaaa8P' 88     88       88    .d8888b. .d8888b. 88 
    88         88   `8b. 88     88       88    88'  `88 88'  `88 88 
    Y8.   .88  88    .88 Y8.   .8P       88    88.  .88 88.  .88 88 
     Y88888P'  88888888P  `8888P'        dP    `88888P' `88888P' dP 
                                                                                                                                                                                                                                                                         
    {Style.RESET_ALL}
    by z3nTr4ry""")
    print(f"    {Fore.GREEN}[+] เบราว์เซอร์จะไม่ปิดจนกว่าจะจบการทำงาน{Style.RESET_ALL}\n")

    scraper = CambridgeOneScraper()
    email, saved_password, should_continue = choose_account(scraper)
    if not should_continue or not email:
        print(f"\n  {Fore.YELLOW}[!] Exiting...{Style.RESET_ALL}")
        return

    try:
        profile_dir = scraper._profile_dir(email)
        print(f"  {Fore.CYAN}[*] กำลังเปิด Chrome data profile: {profile_dir}{Style.RESET_ALL}")
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
                password = getpass.getpass(f"  {Fore.CYAN}[>] Password ของ {email}: {Style.RESET_ALL}").strip()
                if password:
                    try:
                        save_choice = input(f"  {Fore.CYAN}[>] บันทึกรหัสผ่าน? (y/n): {Style.RESET_ALL}").strip().lower()
                    except Exception:
                        save_choice = 'y'

                    if save_choice == 'y':
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
                    input(f"\n  {Fore.CYAN}[>] Press Enter to close browser...{Style.RESET_ALL}")
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
                choice = input(f"\n  {Fore.CYAN}[>] Enter class number: {Style.RESET_ALL}").strip()
            except (EOFError, KeyboardInterrupt):
                choice = 'q'

            if choice.lower() == 'q':
                break
            if choice == '0':
                continue

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(courses):
                    selected_course = courses[idx]
                    success = await scraper.click_workbook(selected_course['courseName'])
                    if not success:
                        continue

                    await run_unit_loop(scraper, selected_course)
                else:
                    print(f"  {Fore.RED}[-] Invalid class number{Style.RESET_ALL}")
            except ValueError:
                print(f"  {Fore.RED}[-] Please enter a valid number{Style.RESET_ALL}")

        try:
            input(f"\n  {Fore.CYAN}[>] Press Enter to close browser...{Style.RESET_ALL}")
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
            unit_choice = input(f"\n  {Fore.CYAN}[>] เลือกบท (ตัวเลข): {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            unit_choice = '0'

        if unit_choice.lower() == 'q':
            sys.exit(0)
        if unit_choice == '0':
            return
        if unit_choice.lower() == 'r':
            continue

        try:
            unit_idx = int(unit_choice) - 1
            if 0 <= unit_idx < len(units):
                selected_unit = units[unit_idx]
                await scraper.click_unit(selected_unit['name'])
                exercises_data = await scraper.get_exercises_in_unit(selected_unit['name'])
                await run_unit_menu(scraper, selected_course, selected_unit, exercises_data)
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
        print(f"\n  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}Session log: {logger.log_file}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 70}{Style.RESET_ALL}\n")

        asyncio.run(main_async())

    except Exception as e:
        print(f"\n  {Fore.RED}[-] Fatal Error: {e}{Style.RESET_ALL}")
        traceback.print_exc()
    finally:
        print(f"\n  {Fore.CYAN}[*] บันทึก log ไปที่: {logger.log_file}{Style.RESET_ALL}")
        logger.close()
        sys.stdout = logger.original_stdout
        sys.stderr = logger.original_stderr


if __name__ == "__main__":
    main()
