"""Standalone Windows CHD difference patcher; source files are read-only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib


def resources() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'payload'
    base = Path(__file__).resolve().parents[1]
    if (base / 'payload/manifest.json').is_file():
        return base / 'payload'
    return base / 'release/v0.8/payload'


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def manifest() -> dict:
    return json.loads((resources() / 'manifest.json').read_text('utf-8'))


def check_file(path: Path, record: dict, label: str) -> str:
    if not path.is_file() or path.stat().st_size != record['size']:
        raise ValueError(f'{label}의 파일 크기가 다릅니다. 배포 안내의 대상 원본을 확인하세요.')
    actual = sha(path)
    if actual != record['sha256']:
        raise ValueError(f'{label}의 SHA-256이 다릅니다.\n확인된 해시: {actual}')
    return actual


def run_tool(tool: Path, arguments: list[str], cwd: Path) -> str:
    result = subprocess.run(
        [str(tool), *map(str, arguments)], capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        cwd=cwd,
    )
    text = (result.stdout + result.stderr).decode('utf-8', 'replace')
    if result.returncode:
        raise RuntimeError('CHD 처리에 실패했습니다.\n' + text[-1800:])
    return text


def read_gdi(path: Path) -> list[dict]:
    lines = path.read_text('utf-8-sig').splitlines()
    if not lines or lines[0].strip() != '3':
        raise ValueError('지원 대상은 일본판 Special Edition 3트랙 원본입니다.')
    tracks = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = shlex.split(line)
        if len(fields) != 6:
            raise ValueError('추출된 트랙 정보가 올바르지 않습니다.')
        filename = fields[4]
        resolved = (path.parent / filename).resolve(strict=True)
        if Path(filename).name != filename or resolved.parent != path.parent.resolve():
            raise ValueError('추출 폴더 밖의 트랙 경로입니다.')
        number, lba, kind, sector, offset = [int(fields[i]) for i in (0, 1, 2, 3, 5)]
        tracks.append(dict(number=number, lba=lba, type=kind, sector=sector, offset=offset, path=resolved))
    if len(tracks) != 3 or [t['number'] for t in tracks] != [1, 2, 3]:
        raise ValueError('추출된 트랙 구성이 올바르지 않습니다.')
    return tracks


def read_sectors(path: Path, m: dict) -> dict[int, tuple[bytes, bytes]]:
    # The bundle has a fixed, verified uncompressed length. Avoid accepting
    # truncated streams, trailing members or unbounded decompression.
    raw = path.read_bytes()
    decoder = zlib.decompressobj()
    data = decoder.decompress(raw, m['patch']['uncompressed_size'] + 1)
    if len(data) != m['patch']['uncompressed_size'] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('내장 섹터 패치가 손상되었습니다.')
    if data[:8] != b'GUNKR080':
        raise ValueError('내장 섹터 패치의 형식이 다릅니다.')
    version, sector_size, total, count = struct.unpack_from('<4I', data, 8)
    if (version, sector_size, total, count) != (1, 2048, m['high_density']['sectors'], m['patch']['changed_sectors']):
        raise ValueError('내장 섹터 패치의 구성이 다릅니다.')
    if len(data) != 24 + count * (4 + 32 + 2048):
        raise ValueError('내장 섹터 패치의 길이가 다릅니다.')
    sectors = {}
    previous = -1
    for n in range(count):
        pos = 24 + n * 2084
        index, old_sha, replacement = struct.unpack_from('<I32s2048s', data, pos)
        if not previous < index < total:
            raise ValueError('섹터 패치의 범위 또는 순서가 잘못되었습니다.')
        sectors[index] = (old_sha, replacement)
        previous = index
    return sectors


def patch_disc(gdi: Path, folder: Path, m: dict, sectors: dict) -> Path:
    tracks = read_gdi(gdi)
    for track, expected in zip(tracks[:2], m['tracks']):
        if any(track[k] != expected[k] for k in ('number', 'lba', 'type', 'sector', 'offset')):
            raise ValueError('원본의 저밀도 트랙 구성이 다릅니다.')
        check_file(track['path'], expected, f"원본 트랙 {track['number']}")
        shutil.copyfile(track['path'], folder / expected['output_file'])
    hd = tracks[2]
    if (hd['lba'], hd['type'], hd['sector'], hd['offset']) != (45000, 4, 2352, 0):
        raise ValueError('원본의 고밀도 트랙 구성이 다릅니다.')
    total = m['high_density']['sectors']
    if hd['path'].stat().st_size != total * 2352:
        raise ValueError('원본 고밀도 트랙의 길이가 다릅니다.')
    before_hash, after_hash = hashlib.sha256(), hashlib.sha256()
    applied = 0
    with hd['path'].open('rb') as source, (folder / 'track03.iso').open('xb') as output:
        for first in range(0, total, 512):
            count = min(512, total - first)
            raw = source.read(count * 2352)
            if len(raw) != count * 2352:
                raise ValueError('고밀도 트랙이 중간에 잘렸습니다.')
            logical = bytearray(b''.join(raw[n + 16:n + 2064] for n in range(0, len(raw), 2352)))
            before_hash.update(logical)
            for n in range(count):
                record = sectors.get(first + n)
                if record is None:
                    continue
                expected, replacement = record
                offset = n * 2048
                if hashlib.sha256(logical[offset:offset + 2048]).digest() != expected:
                    raise ValueError(f'원본 섹터 {first + n}의 해시가 다릅니다.')
                logical[offset:offset + 2048] = replacement
                applied += 1
            output.write(logical)
            after_hash.update(logical)
    if before_hash.hexdigest() != m['high_density']['source_logical_sha256'] or after_hash.hexdigest() != m['high_density']['target_logical_sha256'] or applied != len(sectors):
        raise RuntimeError('고밀도 트랙 전체 해시 또는 적용 섹터 수가 다릅니다.')
    lines = ['3', *[f"{t['number']} {t['lba']} {t['type']} {t['sector']} {t['output_file']} 0" for t in m['tracks']], '3 45000 4 2048 track03.iso 0']
    result = folder / 'patched.gdi'
    result.write_text('\n'.join(lines) + '\n', encoding='ascii')
    # Only remove files produced inside this invocation's private directory.
    # Never remove or modify the user's CHD.
    for track in tracks:
        assert track['path'].parent == folder.resolve()
        track['path'].unlink()
    return result


def apply(source: Path, output: Path, notify=lambda text: None) -> dict:
    m = manifest()
    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError('원본과 다른 출력 파일을 지정하세요. 원본에는 덮어쓰지 않습니다.')
    if output.suffix.lower() != '.chd':
        raise ValueError('출력 파일의 확장자는 .chd로 지정하세요.')
    if output.exists():
        raise ValueError('출력 파일이 이미 있습니다. 새 파일 이름을 지정하세요.')
    notify('1/6  일본판 원본의 크기와 SHA-256 확인 중…')
    if source.is_file() and source.stat().st_size == m['target']['size'] and sha(source) == m['target']['sha256']:
        raise ValueError('이미 v0.8 패치가 적용된 파일입니다. 패치 전 일본판 원본을 선택하세요.')
    source_sha = check_file(source, m['source'], '일본판 원본 CHD')
    notify('2/6  내장 패치와 적용 도구 확인 중…')
    patch = resources() / m['patch']['file']
    tool = resources() / m['tool']['file']
    check_file(patch, m['patch'], '내장 차이 패치')
    check_file(tool, m['tool'], '내장 적용 도구')
    sectors = read_sectors(patch, m)
    if not output.parent.is_dir():
        raise ValueError('존재하는 출력 폴더를 선택하세요.')
    if shutil.disk_usage(output.parent).free < 3 * 1024 * 1024 * 1024:
        raise ValueError('출력 드라이브에 최소 3GB의 여유 공간이 필요합니다.')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='.gundam-kr-v0.8-', dir=output.parent) as folder:
        temporary = Path(folder) / 'translated.chd'
        work = Path(folder)
        notify('3/6  원본 CHD를 임시 작업 공간에서 읽는 중…')
        # chdman's legacy GDI parser does not handle Unicode absolute paths.
        # Every path passed to the tool is an ASCII name relative to its cwd;
        # Python handles the user's Unicode input/output paths and all copies.
        shutil.copyfile(source, work / 'source.chd')
        run_tool(tool, ['extractcd', '-i', 'source.chd', '-o', 'original.gdi'], work)
        (work / 'source.chd').unlink()
        notify('4/6  수정된 게임 데이터 적용 및 전체 트랙 검증 중…')
        gdi = patch_disc(work / 'original.gdi', work, m, sectors)
        notify('5/6  한글 CHD 생성 중… 몇 분 정도 걸릴 수 있습니다.')
        run_tool(tool, ['createcd', '-i', gdi.name, '-o', temporary.name], work)
        notify('6/6  완성된 CHD와 원본 보존 여부 확인 중…')
        verification = run_tool(tool, ['verify', '-i', temporary.name], work)
        target_sha = check_file(temporary, m['target'], '패치 결과 CHD')
        if sha(source) != source_sha:
            raise RuntimeError('작업 중 원본 파일이 변경되었습니다. 결과 생성을 중단합니다.')
        # Windows rename refuses an existing destination. The POSIX fallback
        # uses exclusive hard-link creation to preserve the same guarantee.
        if os.name == 'nt':
            os.rename(temporary, output)
        else:
            os.link(temporary, output)
    proof = dict(
        version=m['version'], success=True, source_sha256=source_sha,
        source_size=m['source']['size'], output_sha256=target_sha,
        output_size=m['target']['size'], output=str(output),
        patch_sha256=m['patch']['sha256'], original_file_unchanged=True,
        changed_sectors=len(sectors), chdman_verification=verification,
        elapsed_seconds=round(time.monotonic() - started, 2),
    )
    receipt = output.with_suffix('.v0.8-patch-result.json')
    if receipt.exists():
        receipt = output.with_name(output.stem + '.' + uuid.uuid4().hex[:8] + '.patch-result.json')
    try:
        with receipt.open('x', encoding='utf-8') as stream:
            json.dump(proof, stream, ensure_ascii=False, indent=2)
        proof['receipt'] = str(receipt)
    except OSError as error:
        # A verified CHD is still a successful patch if the optional receipt
        # cannot be written, e.g. permissions changed after creating the CHD.
        proof['receipt_warning'] = str(error)
    notify('완료: 결과 SHA-256 일치 / 원본 파일 보존 확인')
    return proof


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.messagebox = tk, messagebox
        self.root = root = tk.Tk()
        root.title('건담 외전 · 콜로니가 떨어진 땅에서 한국어 패치 v0.8')
        root.geometry('760x435')
        root.minsize(760, 435)
        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.status = tk.StringVar(value='일본판 Special Edition 원본 CHD를 선택하세요.')
        self.events = queue.Queue()
        self.busy = False
        box = ttk.Frame(root, padding=22)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='건담 외전 — 콜로니가 떨어진 땅에서', font=('맑은 고딕', 17, 'bold')).pack(anchor='w')
        ttk.Label(box, text='Dreamcast 일본판 Special Edition · 한국어 패치 v0.8', font=('맑은 고딕', 11)).pack(anchor='w', pady=(7, 3))
        ttk.Label(box, text='한글화: 기타 깎는 노인', font=('맑은 고딕', 10)).pack(anchor='w', pady=(0, 17))
        self.controls = []
        for label, variable, is_source in [('원본 CHD', self.source, True), ('저장할 CHD', self.output, False)]:
            row = ttk.Frame(box)
            row.pack(fill='x', pady=7)
            ttk.Label(row, text=label, width=12).pack(side='left')
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side='left', expand=True, fill='x')
            def choose(v=variable, original=is_source):
                if original:
                    picked = filedialog.askopenfilename(parent=root, title='패치 전 일본판 Special Edition CHD', filetypes=[('CHD 이미지', '*.chd'), ('모든 파일', '*.*')])
                    if picked:
                        self.source.set(picked)
                        if not self.output.get():
                            self.output.set(str(Path(picked).parent / manifest()['target']['filename']))
                else:
                    picked = filedialog.asksaveasfilename(parent=root, title='한글 CHD 저장 위치', defaultextension='.chd', initialfile=manifest()['target']['filename'], confirmoverwrite=False, filetypes=[('CHD 이미지', '*.chd')])
                    if picked:
                        v.set(picked)
            button = ttk.Button(row, text='선택…', command=choose)
            button.pack(side='left', padx=(8, 0))
            self.controls.extend([entry, button])
        ttk.Label(box, text='원본과 결과 파일의 SHA-256을 자동 확인합니다.\n기존 파일을 덮어쓰지 않으며, Python 설치나 별도 자막 프로그램이 필요하지 않습니다.', wraplength=700).pack(anchor='w', pady=(14, 8))
        self.bar = ttk.Progressbar(box, mode='indeterminate')
        self.bar.pack(fill='x', pady=5)
        ttk.Label(box, textvariable=self.status, wraplength=700).pack(anchor='w', pady=8)
        self.button = ttk.Button(box, text='한국어 패치 적용', command=self.start)
        self.button.pack(anchor='e', pady=6)
        ttk.Button(box, text='사용 도구 · 라이선스', command=self.licenses).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.pump)

    def licenses(self):
        window = self.tk.Toplevel(self.root)
        window.title('사용 도구 · 라이선스')
        window.geometry('780x550')
        from tkinter.scrolledtext import ScrolledText
        text = ScrolledText(window, wrap='word', padx=12, pady=12)
        text.pack(fill='both', expand=True)
        paths = [resources() / 'THIRD_PARTY_NOTICES.md', *sorted((resources() / 'licenses').glob('*.txt'))]
        for path in paths:
            text.insert('end', '\n\n' + path.name + '\n' + '=' * 60 + '\n' + path.read_text('utf-8', errors='replace'))
        text.configure(state='disabled')

    def start(self):
        if self.busy:
            return
        if not self.source.get().strip() or not self.output.get().strip():
            self.messagebox.showerror('파일 선택', '원본 CHD와 저장할 CHD를 모두 지정하세요.', parent=self.root)
            return
        source, output = Path(self.source.get()), Path(self.output.get())
        self.busy = True
        for widget in [*self.controls, self.button]:
            widget.configure(state='disabled')
        self.bar.start(15)
        def worker():
            try:
                result = apply(source, output, lambda text: self.events.put(('log', text)))
                self.events.put(('done', result))
            except Exception as error:
                self.events.put(('error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.status.set(value)
                    continue
                self.busy = False
                self.bar.stop()
                for widget in [*self.controls, self.button]:
                    widget.configure(state='normal')
                if kind == 'done':
                    self.status.set('패치 완료 — 결과 SHA-256과 원본 보존 확인')
                    self.messagebox.showinfo('패치 완료', '한글 CHD 생성 및 해시 검증 완료\n\n' + value['output'], parent=self.root)
                else:
                    self.status.set('패치 중단: ' + value)
                    self.messagebox.showerror('패치 중단', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def close(self):
        if self.busy:
            self.messagebox.showinfo('패치 적용 중', 'CHD 생성과 검증이 끝난 뒤 닫아 주세요.', parent=self.root)
        else:
            self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    result = {}
    try:
        if args.source is not None or args.output is not None:
            if args.source is None or args.output is None:
                raise ValueError('--source와 --output을 함께 지정하세요.')
            result = apply(args.source, args.output)
        elif args.self_test:
            app = App()
            app.root.withdraw()
            app.root.update_idletasks()
            assert not app.busy and len(app.controls) == 4
            assert 'v0.8' in app.root.title()
            assert app.root.winfo_reqwidth() <= 760 and app.root.winfo_reqheight() <= 435
            app.root.destroy()
            result = dict(success=True, gui_constructed=True, gui_requested_size_within_window=True, version=manifest()['version'])
        else:
            App().root.mainloop()
            return 0
    except Exception as error:
        result = dict(success=False, error_type=type(error).__name__, error=str(error))
    if args.report:
        report = args.report.resolve()
        protected = [p.resolve() for p in (args.source, args.output) if p is not None]
        if report in protected:
            return 2
        # Explicit CLI reports never replace an existing file.
        with report.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('success') else 1


if __name__ == '__main__':
    raise SystemExit(main())
