from flask import Flask, render_template, request, jsonify, send_from_directory
import sqlite3
import os
import base64
import json
import re
import requests
import hashlib
import traceback
from openai import OpenAI
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = '/app/uploads'
app.config['IMAGE_CACHE'] = '/data/image_cache'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Load HA add-on options before initializing OpenAI so the API key is available.
# /data/options.json is written by HA Supervisor from the add-on Configuration tab.
_options_path = '/data/options.json'
if os.path.exists(_options_path):
    try:
        with open(_options_path) as _f:
            _opts = json.load(_f)
        if _opts.get('openai_api_key'):
            os.environ['OPENAI_API_KEY'] = _opts['openai_api_key']
    except Exception:
        pass

client = OpenAI()
DB_PATH = '/data/inventory.db'

CATEGORIES = {
    'Fridge': '🧊',
    'Freezer': '❄️',
    'Pantry': '🥫',
    'Bathroom': '🧴',
    'Laundry': '🧺',
    'Cleaning': '🧹',
    'Snacks': '🍿',
    'Beverages': '🥤',
    'Other': '📦'
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs('/data', exist_ok=True)
    os.makedirs('/app/uploads', exist_ok=True)
    os.makedirs(app.config['IMAGE_CACHE'], exist_ok=True)
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT DEFAULT 'Pantry',
            price REAL DEFAULT 0,
            image_url TEXT,
            image_local TEXT,
            status TEXT DEFAULT 'have',
            quantity INTEGER DEFAULT 1,
            store TEXT,
            date_added TEXT,
            date_modified TEXT
        );
        CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            price REAL DEFAULT 0,
            image_url TEXT,
            image_local TEXT,
            store TEXT,
            added_date TEXT,
            FOREIGN KEY(item_id) REFERENCES items(id)
        );
        CREATE TABLE IF NOT EXISTS image_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_hash TEXT UNIQUE,
            query TEXT,
            local_path TEXT,
            source_url TEXT,
            date_cached TEXT
        );
        CREATE TABLE IF NOT EXISTS item_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            store TEXT NOT NULL,
            price REAL NOT NULL,
            date_recorded TEXT NOT NULL
        );
    ''')
    conn.commit()
    conn.close()

def log_price(conn, item_name, store, price):
    """Record price paid per store for price comparison tracking."""
    if store and store not in ('', 'Unknown') and price and price > 0:
        conn.execute(
            'INSERT INTO price_history (item_name, store, price, date_recorded) VALUES (?,?,?,?)',
            [item_name, store, price, datetime.now().isoformat()]
        )

def log_history_event(conn, item_name, event_type):
    """Record a restocked/needed event for consumption cycle tracking."""
    conn.execute(
        'INSERT INTO item_history (item_name, event_type, event_date) VALUES (?,?,?)',
        [item_name, event_type, datetime.now().isoformat()]
    )

def fetch_from_wikipedia(item_name):
    """Try Wikipedia — only accepts results where the title actually matches all query words.
    Prevents e.g. 'apple sauce' from returning a picture of an apple."""
    try:
        search = requests.get(
            'https://en.wikipedia.org/w/api.php',
            params={'action': 'query', 'list': 'search', 'srsearch': item_name,
                    'format': 'json', 'srlimit': 5},
            headers={'User-Agent': 'KitchenIQ/1.0'},
            timeout=6
        )
        if search.status_code != 200:
            return None
        results = search.json().get('query', {}).get('search', [])
        # Only keep words longer than 2 chars to skip noise like "of", "in"
        query_words = [w for w in item_name.lower().split() if len(w) > 2]
        if not query_words:
            return None
        for result in results:
            title_lower = result['title'].lower()
            # All query words must appear as substrings in the title
            if all(w in title_lower for w in query_words):
                page = requests.get(
                    'https://en.wikipedia.org/api/rest_v1/page/summary/' +
                    requests.utils.quote(result['title']),
                    headers={'User-Agent': 'KitchenIQ/1.0'},
                    timeout=6
                )
                if page.status_code == 200:
                    thumb = page.json().get('thumbnail', {}).get('source')
                    if thumb:
                        return thumb
    except Exception:
        pass
    return None

def fetch_product_image(item_name, description='', store=''):
    """Search for a product image: Google (if configured) → Open Food Facts → Wikipedia."""
    query = f"{item_name} {description}".strip()
    query_hash = hashlib.md5(query.lower().encode()).hexdigest()

    # Check cache first
    conn = get_db()
    cached = conn.execute('SELECT local_path, source_url FROM image_cache WHERE query_hash = ?', [query_hash]).fetchone()
    conn.close()

    if cached and cached['local_path'] and os.path.exists(cached['local_path']):
        return cached['local_path'], cached['source_url']

    google_api_key = os.environ.get('GOOGLE_API_KEY')
    google_cx = os.environ.get('GOOGLE_CX')

    image_url = None
    local_path = None

    if google_api_key and google_cx:
        try:
            resp = requests.get(
                'https://www.googleapis.com/customsearch/v1',
                params={
                    'key': google_api_key,
                    'cx': google_cx,
                    'q': query,
                    'searchType': 'image',
                    'num': 1,
                    'imgSize': 'medium',
                    'safe': 'active'
                },
                timeout=5
            )
            data = resp.json()
            if data.get('items'):
                image_url = data['items'][0]['link']
        except Exception:
            pass

    # Open Food Facts — great for branded grocery products
    if not image_url:
        try:
            resp = requests.get(
                'https://world.openfoodfacts.org/cgi/search.pl',
                params={'search_terms': item_name, 'json': 1, 'page_size': 1, 'fields': 'image_front_small_url'},
                timeout=6,
                headers={'User-Agent': 'KitchenIQ/1.0 (home inventory app)'}
            )
            data = resp.json()
            products = data.get('products', [])
            if products and products[0].get('image_front_small_url'):
                image_url = products[0]['image_front_small_url']
        except Exception:
            pass

    # Wikipedia — reliable fallback for generic household/food items
    if not image_url:
        image_url = fetch_from_wikipedia(item_name)

    # Download and cache image if found
    if image_url:
        try:
            img_resp = requests.get(image_url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if img_resp.status_code == 200:
                ext = '.jpg'
                if 'png' in img_resp.headers.get('content-type', ''):
                    ext = '.png'
                elif 'webp' in img_resp.headers.get('content-type', ''):
                    ext = '.webp'
                filename = f"{query_hash}{ext}"
                local_path = os.path.join(app.config['IMAGE_CACHE'], filename)
                with open(local_path, 'wb') as f:
                    f.write(img_resp.content)
        except Exception:
            local_path = None

    # Cache the result
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO image_cache (query_hash, query, local_path, source_url, date_cached) VALUES (?,?,?,?,?)',
        [query_hash, query, local_path, image_url, datetime.now().isoformat()]
    )
    conn.commit()
    conn.close()

    return local_path, image_url

def analyze_receipt_image(image_data, filename, store=''):
    store_hint = f' This receipt is from {store}. Set "store" to "{store}" for every item.' if store and store != 'Unknown' else ' Guess the store name if visible, otherwise use "Unknown".'
    prompt = (
        "Analyze this receipt/shopping screenshot carefully. Extract ALL items purchased.\n\n"
        "For each item return a JSON array with objects containing:\n"
        "- name: clean product name (e.g. 'Whole Milk', 'Tide Pods', 'Bananas')\n"
        "- description: brief description of the product (1 sentence)\n"
        "- price: numeric price as a float (just the number, no $ sign). If not visible use 0.\n"
        "- category: MUST be one of exactly: Fridge, Freezer, Pantry, Bathroom, Laundry, Cleaning, Snacks, Beverages, Other\n"
        "  - Fridge: dairy, deli meat, fresh produce, eggs, juice, yogurt, cheese\n"
        "  - Freezer: frozen meals, ice cream, frozen vegetables/meat\n"
        "  - Pantry: canned goods, pasta, rice, bread, cereal, cooking oils, spices, baking\n"
        "  - Bathroom: toiletries, soap, shampoo, toothpaste, medicine\n"
        "  - Laundry: detergent, fabric softener, dryer sheets\n"
        "  - Cleaning: cleaning sprays, paper towels, trash bags, dish soap\n"
        "  - Snacks: chips, cookies, candy, nuts, crackers\n"
        "  - Beverages: soda, water, coffee, tea, sports drinks, alcohol\n"
        "  - Other: anything else\n"
        f"- store: {store_hint}\n\n"
        "Return ONLY a valid JSON array, no markdown, no explanation. Example:\n"
        '[{"name":"Whole Milk","description":"1 gallon whole milk","price":3.99,"category":"Fridge","store":"Kroger"}]'
    )
    media_type = "image/png" if filename.lower().endswith('.png') else "image/jpeg"
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    text = response.choices[0].message.content.strip()
    text = re.sub(r'^```(?:json)?\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    return json.loads(text)


@app.route('/')
def index():
    # X-Ingress-Path is set by HA Supervisor when running as an add-on.
    # Empty string in standalone mode — JS BASE variable becomes '' and is a no-op.
    ingress_path = request.headers.get('X-Ingress-Path', '').rstrip('/')
    return render_template('index.html', categories=CATEGORIES, ingress_path=ingress_path)

@app.route('/api/items', methods=['GET'])
def get_items():
    category = request.args.get('category', '')
    status = request.args.get('status', '')
    conn = get_db()
    query = 'SELECT * FROM items WHERE 1=1'
    params = []
    if category:
        query += ' AND category = ?'
        params.append(category)
    if status:
        query += ' AND status = ?'
        params.append(status)
    query += ' ORDER BY category, name'
    items = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return jsonify(items)

@app.route('/api/items', methods=['POST'])
def add_item():
    data = request.json
    conn = get_db()
    now = datetime.now().isoformat()
    local_path, image_url = fetch_product_image(data.get('name',''), data.get('description',''), data.get('store',''))
    cursor = conn.execute(
        'INSERT INTO items (name, description, category, price, store, status, image_url, image_local, date_added, date_modified) VALUES (?,?,?,?,?,"have",?,?,?,?)',
        [data.get('name',''), data.get('description',''), data.get('category','Pantry'),
         data.get('price', 0), data.get('store',''), image_url, local_path, now, now]
    )
    log_history_event(conn, data.get('name',''), 'restocked')
    log_price(conn, data.get('name',''), data.get('store',''), data.get('price', 0))
    conn.commit()
    conn.close()
    return jsonify({'id': cursor.lastrowid, 'success': True})

@app.route('/api/items/<int:item_id>', methods=['PUT'])
def update_item(item_id):
    data = request.json
    conn = get_db()
    fields = []
    values = []
    for key in ['name', 'description', 'category', 'price', 'status', 'quantity', 'store']:
        if key in data:
            fields.append(f'{key} = ?')
            values.append(data[key])
    fields.append('date_modified = ?')
    values.append(datetime.now().isoformat())
    values.append(item_id)
    conn.execute(f'UPDATE items SET {", ".join(fields)} WHERE id = ?', values)
    conn.commit()

    if 'price' in data or 'store' in data:
        item = dict(conn.execute('SELECT * FROM items WHERE id = ?', [item_id]).fetchone())
        log_price(conn, item['name'], item.get('store',''), item.get('price', 0))
        conn.commit()

    if data.get('status') == 'needed':
        item = dict(conn.execute('SELECT * FROM items WHERE id = ?', [item_id]).fetchone())
        log_history_event(conn, item['name'], 'needed')
        existing = conn.execute('SELECT id FROM shopping_list WHERE item_id = ?', [item_id]).fetchone()
        if not existing:
            conn.execute(
                'INSERT INTO shopping_list (item_id, name, description, category, price, image_url, image_local, store, added_date) VALUES (?,?,?,?,?,?,?,?,?)',
                [item_id, item['name'], item['description'], item['category'],
                 item['price'], item['image_url'], item['image_local'], item['store'], datetime.now().isoformat()]
            )
            conn.commit()
        else:
            conn.execute(
                'UPDATE shopping_list SET name=?, description=?, category=?, price=?, store=? WHERE item_id=?',
                [item['name'], item['description'], item['category'], item['price'], item['store'], item_id]
            )
            conn.commit()
    elif data.get('status') == 'have':
        conn.execute('DELETE FROM shopping_list WHERE item_id = ?', [item_id])
        conn.commit()

    conn.close()
    return jsonify({'success': True})

@app.route('/api/items/<int:item_id>', methods=['DELETE'])
def delete_item(item_id):
    conn = get_db()
    conn.execute('DELETE FROM shopping_list WHERE item_id = ?', [item_id])
    conn.execute('DELETE FROM items WHERE id = ?', [item_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/items/<int:item_id>/refresh-image', methods=['POST'])
def refresh_image(item_id):
    conn = get_db()
    item = conn.execute('SELECT * FROM items WHERE id = ?', [item_id]).fetchone()
    if not item:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    query = f"{item['name']} {item['description'] or ''}".strip()
    query_hash = hashlib.md5(query.lower().encode()).hexdigest()
    conn.execute('DELETE FROM image_cache WHERE query_hash = ?', [query_hash])
    conn.commit()
    local_path, image_url = fetch_product_image(item['name'], item['description'] or '', item['store'] or '')
    conn.execute('UPDATE items SET image_url=?, image_local=?, date_modified=? WHERE id=?',
                 [image_url, local_path, datetime.now().isoformat(), item_id])
    conn.execute('UPDATE shopping_list SET image_url=?, image_local=? WHERE item_id=?',
                 [image_url, local_path, item_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'image_url': image_url, 'image_local': local_path})

@app.route('/api/items/<int:item_id>/upload-image', methods=['POST'])
def upload_item_image(item_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400
    conn = get_db()
    item = conn.execute('SELECT * FROM items WHERE id = ?', [item_id]).fetchone()
    if not item:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        ext = '.jpg'
    filename = f"custom_{item_id}{ext}"
    local_path = os.path.join(app.config['IMAGE_CACHE'], filename)
    file.save(local_path)
    query = f"{item['name']} {item['description'] or ''}".strip()
    query_hash = hashlib.md5(query.lower().encode()).hexdigest()
    conn.execute(
        'INSERT OR REPLACE INTO image_cache (query_hash, query, local_path, source_url, date_cached) VALUES (?,?,?,?,?)',
        [query_hash, query, local_path, None, datetime.now().isoformat()]
    )
    conn.execute('UPDATE items SET image_local=?, image_url=NULL, date_modified=? WHERE id=?',
                 [local_path, datetime.now().isoformat(), item_id])
    conn.execute('UPDATE shopping_list SET image_local=?, image_url=NULL WHERE item_id=?',
                 [local_path, item_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'image_local': local_path})

@app.route('/api/shopping-list', methods=['GET'])
def get_shopping_list():
    conn = get_db()
    items = [dict(r) for r in conn.execute('SELECT * FROM shopping_list ORDER BY category, name').fetchall()]
    total = sum(i['price'] for i in items if i['price'])
    conn.close()
    return jsonify({'items': items, 'total': round(total, 2)})

@app.route('/api/shopping-list/<int:item_id>', methods=['DELETE'])
def remove_from_shopping_list(item_id):
    conn = get_db()
    row = conn.execute('SELECT item_id FROM shopping_list WHERE id = ?', [item_id]).fetchone()
    if row and row['item_id']:
        conn.execute('UPDATE items SET status = "have", date_modified = ? WHERE id = ?',
                     [datetime.now().isoformat(), row['item_id']])
        purchased = conn.execute('SELECT name FROM items WHERE id = ?', [row['item_id']]).fetchone()
        if purchased:
            log_history_event(conn, purchased['name'], 'restocked')
    conn.execute('DELETE FROM shopping_list WHERE id = ?', [item_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) as c FROM items').fetchone()['c']
    have = conn.execute('SELECT COUNT(*) as c FROM items WHERE status="have"').fetchone()['c']
    needed = conn.execute('SELECT COUNT(*) as c FROM items WHERE status="needed"').fetchone()['c']
    shopping_total = conn.execute('SELECT SUM(price) as t FROM shopping_list').fetchone()['t'] or 0
    conn.close()
    return jsonify({'total': total, 'have': have, 'needed': needed, 'shopping_total': round(shopping_total, 2)})

@app.route('/api/suggestions', methods=['GET'])
def get_suggestions():
    from collections import defaultdict
    conn = get_db()
    rows = conn.execute(
        'SELECT item_name, event_date FROM item_history WHERE event_type="restocked" ORDER BY item_name, event_date'
    ).fetchall()
    restock_map = defaultdict(list)
    for row in rows:
        restock_map[row['item_name']].append(row['event_date'])
    today = datetime.now()
    suggestions = []
    for item_name, dates in restock_map.items():
        if len(dates) < 2:
            continue
        parsed = sorted([datetime.fromisoformat(d) for d in dates])
        cycles = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
        avg_cycle = sum(cycles) / len(cycles)
        if avg_cycle < 1:
            continue
        last_restock = parsed[-1]
        days_since = (today - last_restock).days
        days_until = round(avg_cycle - days_since)
        item = conn.execute(
            'SELECT * FROM items WHERE name=? COLLATE NOCASE ORDER BY date_modified DESC LIMIT 1',
            [item_name]
        ).fetchone()
        suggestions.append({
            'item_name': item_name,
            'category': item['category'] if item else 'Other',
            'image_local': item['image_local'] if item else None,
            'image_url': item['image_url'] if item else None,
            'item_id': item['id'] if item else None,
            'status': item['status'] if item else None,
            'avg_cycle_days': round(avg_cycle),
            'days_since_restock': days_since,
            'days_until_needed': days_until,
            'confidence': round(min(len(dates) / 5.0, 1.0), 2),
            'restock_count': len(dates)
        })
    conn.close()
    suggestions.sort(key=lambda x: x['days_until_needed'])
    return jsonify(suggestions)

@app.route('/api/items/<int:item_id>/price-history', methods=['GET'])
def get_price_history(item_id):
    conn = get_db()
    item = conn.execute('SELECT name FROM items WHERE id = ?', [item_id]).fetchone()
    if not item:
        conn.close()
        return jsonify([])
    # Most recent price per store, sorted cheapest first
    rows = conn.execute('''
        SELECT ph.store, ph.price, ph.date_recorded
        FROM price_history ph
        INNER JOIN (
            SELECT store, MAX(date_recorded) as max_date
            FROM price_history
            WHERE item_name = ? COLLATE NOCASE
            GROUP BY store
        ) latest ON ph.store = latest.store AND ph.date_recorded = latest.max_date
        WHERE ph.item_name = ? COLLATE NOCASE
        ORDER BY ph.price ASC
    ''', [item['name'], item['name']]).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    if result:
        result[0]['cheapest'] = True
    return jsonify(result)

@app.route('/api/upload-receipt', methods=['POST'])
def upload_receipt():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    store = request.form.get('store', '')
    with open(filepath, 'rb') as f:
        image_data = base64.standard_b64encode(f.read()).decode('utf-8')
    try:
        items = analyze_receipt_image(image_data, filename, store)
        # Fetch all images before opening the main DB connection.
        # fetch_product_image opens its own connection to write to image_cache,
        # which causes "database is locked" if the main connection is already open.
        images = {
            item.get('name', ''): fetch_product_image(item.get('name', ''), item.get('description', ''), item.get('store', ''))
            for item in items
        }
        conn = get_db()
        added = []
        for item in items:
            now = datetime.now().isoformat()
            local_path, image_url = images.get(item.get('name', ''), (None, None))
            existing = conn.execute('SELECT id FROM items WHERE name = ? COLLATE NOCASE', [item['name']]).fetchone()
            if existing:
                conn.execute(
                    'UPDATE items SET status="have", quantity=quantity+1, date_modified=?, image_url=COALESCE(NULLIF(image_url,""),?), image_local=COALESCE(NULLIF(image_local,""),?) WHERE id=?',
                    [now, image_url, local_path, existing['id']]
                )
                conn.execute('DELETE FROM shopping_list WHERE item_id = ?', [existing['id']])
                log_history_event(conn, item['name'], 'restocked')
                log_price(conn, item['name'], item.get('store', store), item.get('price', 0))
                added.append({'id': existing['id'], 'name': item['name'], 'action': 'updated'})
            else:
                cursor = conn.execute(
                    'INSERT INTO items (name, description, category, price, store, status, image_url, image_local, date_added, date_modified) VALUES (?,?,?,?,?,"have",?,?,?,?)',
                    [item.get('name','Unknown'), item.get('description',''), item.get('category','Pantry'),
                     item.get('price', 0), item.get('store', store) or store, image_url, local_path, now, now]
                )
                log_history_event(conn, item.get('name','Unknown'), 'restocked')
                log_price(conn, item.get('name','Unknown'), item.get('store', store) or store, item.get('price', 0))
                added.append({'id': cursor.lastrowid, 'name': item['name'], 'action': 'added'})
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'items': added, 'count': len(added)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/image-cache/<filename>')
def cached_image(filename):
    return send_from_directory(app.config['IMAGE_CACHE'], filename)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/ha/push-shopping-list', methods=['POST'])
def push_to_ha_shopping_list():
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        return jsonify({'error': 'Not running as HA add-on (no SUPERVISOR_TOKEN)'}), 400
    conn = get_db()
    items = [dict(r) for r in conn.execute('SELECT * FROM shopping_list ORDER BY category, name').fetchall()]
    conn.close()
    headers = {
        'Authorization': f'Bearer {supervisor_token}',
        'Content-Type': 'application/json'
    }
    pushed = 0
    errors = 0
    for item in items:
        try:
            resp = requests.post(
                'http://supervisor/core/api/shopping_list/item',
                json={'name': item['name']},
                headers=headers,
                timeout=5
            )
            if resp.status_code in (200, 201):
                pushed += 1
            else:
                errors += 1
        except Exception:
            errors += 1
    return jsonify({'success': True, 'pushed': pushed, 'errors': errors})

def load_ha_options():
    """If running as an HA add-on, /data/options.json holds user-configured values."""
    options_path = '/data/options.json'
    if os.path.exists(options_path):
        try:
            with open(options_path) as f:
                opts = json.load(f)
            if opts.get('openai_api_key'):
                os.environ['OPENAI_API_KEY'] = opts['openai_api_key']
        except Exception:
            pass

if __name__ == '__main__':
    load_ha_options()
    init_db()
    app.run(debug=False, host='0.0.0.0', port=5000)
