"""Read-only scanner and parser for active project status files (项目状态.md)."""
import datetime
import os
import re
import time

from dashboard_quota import load_project_config

STATUS_FILENAME = '项目状态.md'
MAX_FILE_BYTES = 256 * 1024

DEFAULT_SCAN_ROOTS = [
    {'label': 'ZHITAI', 'path': '/Volumes/ZHITAI/codex_project'},
    {'label': 'OpenClaw', 'path': os.path.expanduser('~/.openclaw/workspace/ai_workspace/projects')},
]
DEFAULT_PENDING_RULES_PATH = '/Volumes/ZHITAI/codex_project/虾厂共同记忆/待审规则建议.md'

EXCLUDED_DIR_NAMES = {
    'tmp',
    'TMP',
    'archive',
    '_归档',
    'node_modules',
    '__pycache__',
    '虾厂共同记忆',
}
FORBIDDEN_PATH_SUBSTRINGS = ('workspace-xiaohexia',)
STATUS_ORDER = {
    'decision': 0,
    'active': 1,
    'legacy': 2,
    'paused': 3,
    'done': 4,
}
TZ_BEIJING = datetime.timezone(datetime.timedelta(hours=8))

_HEADER_BULLET_RE = re.compile(r'^\s*[-*]\s*\*\*(.+?)\*\*\s*[：:]\s*(.+?)\s*$')
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_DATETIME_RE = re.compile(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?')
_CN_DATE_RE = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日')
_HEADING_DATE_PREFIX_RE = re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}[\s｜|·\-—:：]*')
_HEADING_DATE_SUFFIX_RE = re.compile(r'[\s｜|·\-—:：]*\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:（[^）]*）|\([^)]*\))?$')
_TOP_UPDATED_RE = re.compile(r'更新(?:时间)?[：:]\s*([^\n。；]+)')
_NEXT_LINE_RE = re.compile(r'^\s*(?:[-*]\s*)?(?:\*\*)?下一步(?:\*\*)?[：:]\s*(.+)$')


def strip_markdown_inline(text):
    """Strip common Markdown inline formatting while keeping readable text."""
    if not text:
        return ''
    cleaned = _MD_LINK_RE.sub(r'\1', str(text))
    cleaned = cleaned.replace('`', '')
    cleaned = cleaned.replace('**', '').replace('__', '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _truncate(text, limit=180):
    text = strip_markdown_inline(text)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + '…'


def normalize_status(raw_status):
    """Normalize free-form status string to (statusKey, statusLabel)."""
    clean = strip_markdown_inline(raw_status)
    if not clean:
        return 'legacy', '纪要式'

    primary = re.split(r'[（(]', clean, maxsplit=1)[0].strip()
    rules = (
        ('decision', '待拍板', ('拍板', '待确认', '待审批', '待决策')),
        ('paused', '暂停', ('暂停', '搁置', '挂起')),
        ('done', '已完结', ('完结', '已完成', '归档')),
        ('active', '进行中', ('进行中', '推进中', '已初始化', '活跃')),
    )
    for target in (primary, clean):
        if not target:
            continue
        for key, label, keywords in rules:
            if any(k in target for k in keywords):
                return key, label
    return 'active', clean[:16]


def parse_date_to_ms(text):
    """Parse YYYY-MM-DD [HH:MM] or YYYY年M月D日 in Asia/Shanghai to (epoch_ms, has_time)."""
    if not text:
        return None, False
    s = str(text)
    m = _DATETIME_RE.search(s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) is not None else 0
        minute = int(m.group(5)) if m.group(5) is not None else 0
        has_time = m.group(4) is not None
        try:
            dt = datetime.datetime(year, month, day, hour, minute, tzinfo=TZ_BEIJING)
            return int(dt.timestamp() * 1000), has_time
        except (ValueError, OverflowError):
            return None, False

    m_cn = _CN_DATE_RE.search(s)
    if m_cn:
        year, month, day = int(m_cn.group(1)), int(m_cn.group(2)), int(m_cn.group(3))
        try:
            dt = datetime.datetime(year, month, day, tzinfo=TZ_BEIJING)
            return int(dt.timestamp() * 1000), False
        except (ValueError, OverflowError):
            return None, False

    return None, False


def _format_ms_beijing(ms):
    if ms is None:
        return ''
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, TZ_BEIJING)
    return dt.strftime('%Y-%m-%d %H:%M')


def _same_beijing_day(ms_a, ms_b):
    if ms_a is None or ms_b is None:
        return False
    da = datetime.datetime.fromtimestamp(ms_a / 1000.0, TZ_BEIJING).date()
    db = datetime.datetime.fromtimestamp(ms_b / 1000.0, TZ_BEIJING).date()
    return da == db


def _extract_sections(lines):
    """Split markdown lines into list of (heading_title, body_lines) for ## headings."""
    sections = []
    cur_title = None
    cur_body = []
    for line in lines:
        if line.startswith('## '):
            if cur_title is not None:
                sections.append((cur_title, cur_body))
            cur_title = line[3:].strip()
            cur_body = []
        elif cur_title is not None:
            cur_body.append(line)
    if cur_title is not None:
        sections.append((cur_title, cur_body))
    return sections


def _first_prose_sentence(lines):
    """Extract first meaningful cleaned prose line/sentence from body lines."""
    for raw in lines:
        s = raw.strip()
        if not s or s == '---' or s.startswith('#'):
            continue
        if s.startswith('|'):
            # Skip markdown table borders / header separators
            if set(s) <= set('|-: ') or '---' in s:
                continue
            continue
        if s.startswith('>'):
            s = s.lstrip('>').strip()
        if s.startswith(('- ', '* ')):
            s = s[2:].strip()
        elif re.match(r'^\d+\.\s+', s):
            s = re.sub(r'^\d+\.\s+', '', s)
        cleaned = strip_markdown_inline(s)
        if not cleaned:
            continue
        # Take up to first sentence stop if very long
        m = re.match(r'^(.{12,120}?[。！？])', cleaned)
        if m:
            return m.group(1)
        return cleaned
    return ''


def _clean_dated_heading(title):
    cleaned = strip_markdown_inline(title)
    cleaned = _HEADING_DATE_PREFIX_RE.sub('', cleaned)
    cleaned = _HEADING_DATE_SUFFIX_RE.sub('', cleaned)
    return cleaned.strip(' ｜|·-—:：')


def _infer_legacy_owner(dir_name, top_lines):
    if '水母' in dir_name:
        return '水母 🪼'
    if '八爪鱼' in dir_name:
        return '八爪鱼 🐙'
    top_text = '\n'.join(top_lines)
    if any(k in top_text for k in ('Codex八爪鱼牵头', '由Codex牵头', 'Codex牵头')):
        return '八爪鱼 / Codex'
    return ''


def _extract_legacy_fields(lines, dir_name, mtime_ms):
    top_lines = lines[:10]
    sections = _extract_sections(lines)

    # Updated time fallback
    updated_text = ''
    for line in top_lines:
        m = _TOP_UPDATED_RE.search(line)
        if m:
            updated_text = strip_markdown_inline(m.group(1))
            break

    dated_sections = []
    for idx, (title, body) in enumerate(sections):
        dm = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', title)
        if dm:
            date_norm = f'{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}'
            dated_sections.append((idx, date_norm, title, body))

    if not updated_text and dated_sections:
        updated_text = max(item[1] for item in dated_sections)
    if not updated_text and mtime_ms is not None:
        updated_text = _format_ms_beijing(mtime_ms)

    # Stopped-at summary fallback
    stopped_at = ''
    if dated_sections:
        max_date = max(item[1] for item in dated_sections)
        first_date = dated_sections[0][1]
        last_date = dated_sections[-1][1]
        if first_date > last_date:
            chosen = next(item for item in dated_sections if item[1] == max_date)
        elif first_date < last_date:
            chosen = [item for item in dated_sections if item[1] == max_date][-1]
        else:
            same_date = [item for item in dated_sections if item[1] == max_date]
            if '初始化' in same_date[0][2]:
                chosen = same_date[-1]
            else:
                chosen = same_date[0]
        clean_head = _clean_dated_heading(chosen[2])
        sentence = _first_prose_sentence(chosen[3])
        if clean_head and sentence:
            stopped_at = f'{clean_head} — {sentence}'
        else:
            stopped_at = clean_head or sentence
    else:
        for line in top_lines:
            if '更新' in line and ('。' in line or '；' in line):
                # Take prose after the date sentence if present
                parts = re.split(r'[。；]', strip_markdown_inline(line), maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    stopped_at = parts[1].strip()
                    break
        if not stopped_at:
            target_sec = next((body for title, body in sections if '当前目标' in title or '当前阶段' in title), None)
            if target_sec:
                stopped_at = _first_prose_sentence(target_sec)
        if not stopped_at:
            non_h1 = [ln for ln in lines if not ln.lstrip().startswith('# ')]
            stopped_at = _first_prose_sentence(non_h1)

    # Next-step fallback
    next_step = ''
    for title, body in sections:
        if '下次聊天建议' in title:
            next_step = _first_prose_sentence(body)
            if next_step:
                break
    if not next_step:
        for line in lines:
            m = _NEXT_LINE_RE.match(line)
            if m:
                next_step = strip_markdown_inline(m.group(1))
                break

    return _infer_legacy_owner(dir_name, top_lines), updated_text, _truncate(stopped_at, 140), _truncate(next_step, 140)


def parse_project_status_markdown(text, dir_name, source_label, mtime_ms=None):
    """Parse standard or legacy 项目状态.md content into normalized card dict."""
    lines = (text or '').splitlines()
    meta = {}
    for line in lines[:35]:
        if line.startswith('## '):
            break
        m = _HEADER_BULLET_RE.match(line)
        if not m:
            continue
        key = strip_markdown_inline(m.group(1))
        val = strip_markdown_inline(m.group(2))
        if key == '项目':
            meta.setdefault('name', val)
        elif key in ('牵头端与负责人', '负责人', '牵头端'):
            meta.setdefault('owner', val)
        elif key in ('状态', '当前状态'):
            meta.setdefault('status_raw', val)
        elif key in ('最后更新', '更新时间'):
            meta.setdefault('updated_text', val)
        elif key in ('停在哪', '当前阶段'):
            meta.setdefault('stopped_at', val)
        elif key == '下一步':
            meta.setdefault('next_step', val)

    name = meta.get('name') or dir_name
    owner = meta.get('owner', '')
    status_raw = meta.get('status_raw', '')
    updated_text = meta.get('updated_text', '')
    stopped_at = meta.get('stopped_at', '')
    next_step = meta.get('next_step', '')

    is_standard = bool(status_raw or (stopped_at and next_step))
    format_mode = 'standard' if is_standard else 'legacy'
    status_key, status_label = normalize_status(status_raw)
    status_detail = status_raw if status_raw and status_raw != status_label else ''

    if not is_standard or not updated_text or not stopped_at:
        leg_owner, leg_updated, leg_stopped, leg_next = _extract_legacy_fields(lines, dir_name, mtime_ms)
        if not owner:
            owner = leg_owner
        if not updated_text:
            updated_text = leg_updated
        if not stopped_at:
            stopped_at = leg_stopped
        if not next_step:
            next_step = leg_next

    parsed_ms, has_time = parse_date_to_ms(updated_text)
    if parsed_ms is None:
        updated_ts_ms = mtime_ms
    elif not has_time and mtime_ms is not None and _same_beijing_day(parsed_ms, mtime_ms):
        updated_ts_ms = mtime_ms
    else:
        updated_ts_ms = parsed_ms

    return {
        'id': f'{source_label}:{dir_name}',
        'name': _truncate(name, 80),
        'dirName': dir_name,
        'source': source_label,
        'owner': _truncate(owner, 64),
        'statusKey': status_key,
        'statusLabel': status_label,
        'statusDetail': _truncate(status_detail, 64),
        'formatMode': format_mode,
        'updatedText': _truncate(updated_text, 48),
        'updatedTsMs': updated_ts_ms,
        'mtimeMs': mtime_ms,
        'stoppedAt': _truncate(stopped_at, 180),
        'nextStep': _truncate(next_step, 180),
    }


def count_pending_rules(rules_path=DEFAULT_PENDING_RULES_PATH):
    """Read shared 待审规则建议.md and count pending rule rows (excluding example rows)."""
    if not rules_path or any(sub in rules_path for sub in FORBIDDEN_PATH_SUBSTRINGS):
        return {'available': False, 'pendingCount': 0}
    try:
        if not os.path.isfile(rules_path):
            return {'available': False, 'pendingCount': 0}
        with open(rules_path, 'rb') as f:
            raw = f.read(MAX_FILE_BYTES).decode('utf-8', errors='replace')
    except OSError:
        return {'available': False, 'pendingCount': 0}

    count = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        cells = [c.strip() for c in stripped.strip('|').split('|')]
        non_empty = [c for c in cells if c]
        if not non_empty:
            continue
        if all(set(c) <= set('-: ') for c in non_empty):
            continue
        last_cell = non_empty[-1]
        if last_cell == '状态' or '示例' in last_cell or '示例' in stripped:
            continue
        if '待审' in last_cell:
            count += 1
    return {'available': True, 'pendingCount': count}


def _is_forbidden_path(path):
    if not path:
        return True
    candidates = [path]
    try:
        candidates.append(os.path.realpath(path))
    except OSError:
        pass
    return any(sub in cand for cand in candidates for sub in FORBIDDEN_PATH_SUBSTRINGS)


def _resolve_scan_roots(scan_roots, project_dir):
    if scan_roots is not None:
        raw_roots = scan_roots
    else:
        raw_roots = DEFAULT_SCAN_ROOTS
        if project_dir:
            cfg = load_project_config(project_dir)
            cfg_roots = cfg.get('project_scan_roots')
            if isinstance(cfg_roots, list) and cfg_roots:
                raw_roots = cfg_roots

    resolved = []
    for item in raw_roots:
        if isinstance(item, dict) and item.get('path'):
            resolved.append({
                'label': str(item.get('label') or os.path.basename(item['path']) or 'root'),
                'path': os.path.expanduser(str(item['path'])),
            })
        elif isinstance(item, str) and item.strip():
            p = os.path.expanduser(item.strip())
            resolved.append({'label': os.path.basename(p) or 'root', 'path': p})
    return resolved


def scan_active_projects(
    scan_roots=None,
    project_dir=None,
    pending_rules_path=DEFAULT_PENDING_RULES_PATH,
    now_ms=None,
):
    """Scan immediate subdirectories of configured roots for 项目状态.md."""
    roots_cfg = _resolve_scan_roots(scan_roots, project_dir)
    projects = []
    roots_status = []
    seen_realpaths = set()

    for root in roots_cfg:
        label = root['label']
        root_path = root['path']
        if _is_forbidden_path(root_path) or not os.path.isdir(root_path):
            roots_status.append({'label': label, 'available': False, 'count': 0})
            continue

        root_count = 0
        try:
            with os.scandir(root_path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            roots_status.append({'label': label, 'available': False, 'count': 0})
            continue

        for entry in entries:
            if entry.name.startswith('.') or entry.name in EXCLUDED_DIR_NAMES:
                continue
            try:
                if not entry.is_dir():
                    continue
                if _is_forbidden_path(entry.path):
                    continue
                real_dir = os.path.realpath(entry.path)
                if real_dir in seen_realpaths:
                    continue
                status_file = os.path.join(entry.path, STATUS_FILENAME)
                if not os.path.isfile(status_file):
                    continue
                seen_realpaths.add(real_dir)
                st = os.stat(status_file)
                mtime_ms = int(st.st_mtime * 1000)
                with open(status_file, 'rb') as f:
                    text = f.read(MAX_FILE_BYTES).decode('utf-8', errors='replace')
            except OSError:
                continue

            item = parse_project_status_markdown(text, entry.name, label, mtime_ms=mtime_ms)
            projects.append(item)
            root_count += 1

        roots_status.append({'label': label, 'available': True, 'count': root_count})

    projects.sort(key=lambda p: (STATUS_ORDER.get(p['statusKey'], 9), -(p['updatedTsMs'] or 0), p['name']))

    counts = {
        'total': len(projects),
        'decision': sum(1 for p in projects if p['statusKey'] == 'decision'),
        'active': sum(1 for p in projects if p['statusKey'] == 'active'),
        'legacy': sum(1 for p in projects if p['statusKey'] == 'legacy'),
        'paused': sum(1 for p in projects if p['statusKey'] == 'paused'),
        'done': sum(1 for p in projects if p['statusKey'] == 'done'),
    }

    return {
        'projects': projects,
        'counts': counts,
        'roots': roots_status,
        'pendingRules': count_pending_rules(pending_rules_path),
        'scannedAt': int(now_ms if now_ms is not None else time.time() * 1000),
    }
