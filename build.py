#!/usr/bin/env python3
"""Сборка сайта ШТАБ: из одного исходника src/index.html — настоящие страницы.

Зачем: сайт написан как одностраничник, страницы переключались хэшем (#cases).
Поисковик считает такой сайт одним адресом, поэтому в индекс попадала только
главная. Сборщик открывает исходник в headless Chrome по каждому хэшу, забирает
готовую разметку, вырезает из неё чужие секции и раскладывает по адресам:

    /                       главная
    /cases/                 список проектов
    /cases/<слаг>/          карточка проекта, по одной на кейс
    /services/  /about/  /contacts/  /privacy/

Запуск:  python3 build.py
"""

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'src', 'index.html')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PORT = 8899

# Адрес сайта. На своём домене — SITE = 'https://shtab.ru', BASE = ''.
SITE = 'https://ivannad11.github.io'
BASE = '/shtab-site'
TODAY = date.today().isoformat()

# Страницы: хэш в исходнике → каталог, заголовок, описание, приоритет в sitemap
PAGES = [
    ('',          '',           0.8,
     'ШТАБ — организация форумов, конференций и образовательных программ',
     'Организуем деловые, образовательные и молодёжные события: программа и спикеры, '
     'персонал на площадку, мультимедиа, съёмка и отчётность. Концепция и смета за три рабочих дня.'),
    ('cases',     'cases',      0.9,
     'Проекты ШТАБ — форумы, интенсивы, образовательные программы',
     'Реализованные проекты: арт-школа продюсирования ИИ, форум «Горький», сбор смены на интенсив, '
     'школа медиаволонтёров, серия городских мероприятий, юбилей сети.'),
    ('services',  'services',   0.9,
     'Услуги: организация форумов, персонал на площадку, мультимедиа — ШТАБ',
     'Программа и спикеры, хостес и координаторы, контент для экранов, интерактивные стенды, '
     'корпоративные фильмы, отчётные документы и закрывающие акты.'),
    ('about',     'about',      0.6,
     'Команда ШТАБ — продюсеры деловых и образовательных событий',
     'Постоянный состав: продюсирование, программная дирекция, работа на площадке, производство контента. '
     'Под каждый проект команда расширяется под задачу.'),
    ('contacts',  'contacts',   0.7,
     'Контакты ШТАБ — запрос на организацию мероприятия',
     'Оставьте имя и телефон — перезвоним в течение рабочего дня. Телефон, почта и Telegram для запроса '
     'на организацию форума, конференции или образовательной программы.'),
]


def cases_from_source(html):
    """Слаг, название, заказчик и лид каждого кейса — прямо из массива CASES.

    Записи нарезаются по началу следующего кейса: внутри есть вложенные массивы
    с фотографиями и фактами, поэтому по закрывающей скобке делить нельзя.
    """
    marks = [m for m in re.finditer(r"\{ n: '(\d+)', slug: '([^']+)',", html)]
    out = []
    for i, m in enumerate(marks):
        block = html[m.end(): marks[i + 1].start() if i + 1 < len(marks) else m.end() + 4000]
        def field(name):
            f = re.search(r"\b%s: '((?:[^'\\]|\\.)*)'" % name, block)
            return f.group(1).replace("\\'", "'") if f else ''
        out.append({
            'n': m.group(1), 'slug': m.group(2), 'title': field('title'),
            'client': field('client'), 'lead': field('lead'), 'type': field('type'),
        })
    return out


def serve(directory):
    """Локальный сервер для пре-рендера. Порт подбирается свободный."""
    global PORT
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **kw)
    socketserver.TCPServer.allow_reuse_address = True
    for port in range(PORT, PORT + 20):
        try:
            httpd = socketserver.TCPServer(('127.0.0.1', port), handler)
        except OSError:
            continue
        PORT = port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    raise RuntimeError('не нашёл свободный порт для сборки')


def render(hash_part):
    """Отдать разметку страницы после того, как отработал скрипт."""
    url = f'http://127.0.0.1:{PORT}/index.html'
    if hash_part:
        url += '#' + hash_part
    res = subprocess.run(
        [CHROME, '--headless', '--disable-gpu', '--virtual-time-budget=6000', '--dump-dom', url],
        capture_output=True, text=True, timeout=120)
    if len(res.stdout) < 5000:
        raise RuntimeError(f'пустой рендер для #{hash_part}: {res.stderr[:300]}')
    return res.stdout


def only_page(html, keep_id):
    """Оставить одну секцию страницы, остальные вырезать вместе с содержимым."""
    for pid in ['p-home', 'p-cases', 'p-case', 'p-services', 'p-about', 'p-contacts']:
        if pid == keep_id:
            html = re.sub(r'(<div id="%s")[^>]*>' % pid, r'\1>', html, count=1)
            continue
        start = html.find('<div id="%s"' % pid)
        if start < 0:
            continue
        i, depth = html.find('>', start) + 1, 1
        while depth and i < len(html):
            nxt_open = html.find('<div', i)
            nxt_close = html.find('</div>', i)
            if nxt_close < 0:
                break
            if 0 <= nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                depth -= 1
                i = nxt_close + 6
        html = html[:start] + html[i:]
    return html


def head_tags(title, desc, canonical, jsonld, og_image):
    ld = '\n'.join('<script type="application/ld+json">%s</script>' %
                   json.dumps(x, ensure_ascii=False, separators=(',', ':')) for x in jsonld)
    return f"""<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="ШТАБ">
<meta property="og:locale" content="ru_RU">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{og_image}">
<meta name="twitter:card" content="summary_large_image">
{ld}"""


def org_ld():
    return {
        '@context': 'https://schema.org', '@type': 'Organization', 'name': 'ШТАБ',
        'alternateName': 'SHTAB',
        'url': SITE + BASE + '/',
        'description': 'Организация деловых, образовательных и молодёжных мероприятий: '
                       'программа и спикеры, персонал на площадку, мультимедиа, съёмка и отчётность.',
        'telephone': '+7 930 819-76-46', 'email': 'ivannad11@yandex.ru',
        'address': {'@type': 'PostalAddress', 'addressLocality': 'Москва', 'addressCountry': 'RU'},
        'areaServed': 'RU',
        'sameAs': ['https://t.me/fillius_fortunae'],
    }


def crumbs(items):
    return {'@context': 'https://schema.org', '@type': 'BreadcrumbList',
            'itemListElement': [{'@type': 'ListItem', 'position': i + 1, 'name': n,
                                 'item': SITE + BASE + u} for i, (n, u) in enumerate(items)]}


def write(path_parts, html):
    out_dir = os.path.join(ROOT, *path_parts)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    return '/'.join(path_parts) + '/index.html' if path_parts else 'index.html'


def finish(html, page_attr, case_attr, title, desc, canonical, jsonld, og_image):
    """Общая доводка: мета в <head>, отметка страницы в <body>, чистка."""
    # старые мета-теги долой — вместо них свои
    for pat in [r'<title>.*?</title>', r'<meta name="description"[^>]*>', r'<meta name="robots"[^>]*>',
                r'<meta property="og:[^"]*"[^>]*>', r'<meta name="twitter:card"[^>]*>',
                r'<link rel="canonical"[^>]*>']:
        html = re.sub(pat, '', html, flags=re.S)
    html = html.replace('</head>', head_tags(title, desc, canonical, jsonld, og_image) + '\n</head>', 1)

    attrs = f' data-page="{page_attr}"'
    if case_attr:
        attrs += f' data-case="{case_attr}"'
    html = re.sub(r'<body([^>]*)>', lambda m: f'<body{m.group(1)}{attrs}>', html, count=1)

    # адреса ресурсов — от корня сайта, иначе на /cases/ они ищутся в подпапке
    html = html.replace('{{BASE}}', BASE)
    html = re.sub(r'(src|href)="(img/|files/)', lambda m: f'{m.group(1)}="{BASE}/{m.group(2)}', html)
    html = re.sub(r"'(img/|files/)", lambda m: f"'{BASE}/{m.group(1)}", html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html


def main():
    src_html = open(SRC, encoding='utf-8').read()
    cases = cases_from_source(src_html)
    print(f'кейсов в исходнике: {len(cases)}')

    httpd = serve(os.path.join(ROOT, 'src'))
    # картинки и файлы лежат в корне — на время сборки прокинем их в src/
    for extra in ('img', 'files'):
        link = os.path.join(ROOT, 'src', extra)
        if not os.path.exists(link):
            os.symlink(os.path.join(ROOT, extra), link)

    written, urls = [], []
    og_default = f'{SITE}{BASE}/img/case-gorky.jpg'
    try:
        for hash_part, folder, prio, title, desc in PAGES:
            html = render(hash_part)
            keep = 'p-' + (folder or 'home')
            html = only_page(html, keep)
            path = '/' + (folder + '/' if folder else '')
            ld = [org_ld()]
            if folder:
                ld.append(crumbs([('Главная', '/'), (title.split(' —')[0].split(':')[0], path)]))
            html = finish(html, folder or 'home', None, title, desc, SITE + BASE + path, ld, og_default)
            written.append(write([folder] if folder else [], html))
            urls.append((path, prio))
            print(f'  собрано {path}')

        for c in cases:
            html = render('case/' + c['n'])
            html = only_page(html, 'p-case')
            path = f"/cases/{c['slug']}/"
            title = f"{c['title']} — кейс ШТАБ"
            desc = f"{c['client']}. {c['lead']}"[:290]
            ld = [org_ld(),
                  crumbs([('Главная', '/'), ('Проекты', '/cases/'), (c['title'], path)]),
                  {'@context': 'https://schema.org', '@type': 'CreativeWork', 'name': c['title'],
                   'about': c['type'], 'description': c['lead'],
                   'url': SITE + BASE + path,
                   'creator': {'@type': 'Organization', 'name': 'ШТАБ'},
                   'sponsor': {'@type': 'Organization', 'name': c['client']}}]
            html = finish(html, 'case', c['n'], title, desc, SITE + BASE + path, ld, og_default)
            written.append(write(['cases', c['slug']], html))
            urls.append((path, 0.7))
            print(f'  собрано {path}')
    finally:
        httpd.shutdown()
        for extra in ('img', 'files'):
            link = os.path.join(ROOT, 'src', extra)
            if os.path.islink(link):
                os.unlink(link)

    # страница про обработку данных — отдельным шаблоном, без интерактива
    privacy = open(os.path.join(ROOT, 'src', 'privacy.html'), encoding='utf-8').read()
    privacy = privacy.replace('{{BASE}}', BASE).replace('{{DATE}}', TODAY)
    write(['privacy'], privacy)
    urls.append(('/privacy/', 0.3))
    print('  собрано /privacy/')

    # 404: GitHub Pages отдаёт этот файл на несуществующие адреса
    shutil.copy(os.path.join(ROOT, 'index.html'), os.path.join(ROOT, '404.html'))

    with open(os.path.join(ROOT, 'sitemap.xml'), 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for path, prio in sorted(urls, key=lambda x: -x[1]):
            f.write(f'  <url><loc>{SITE}{BASE}{path}</loc><lastmod>{TODAY}</lastmod>'
                    f'<priority>{prio}</priority></url>\n')
        f.write('</urlset>\n')

    with open(os.path.join(ROOT, 'robots.txt'), 'w', encoding='utf-8') as f:
        f.write('User-agent: *\nAllow: /\n\n'
                f'Sitemap: {SITE}{BASE}/sitemap.xml\n')

    print(f'\nготово: {len(written)} страниц, sitemap на {len(urls)} адресов')


if __name__ == '__main__':
    main()
