"""Generate the public feed; refuse to publish a snapshot missing expected media."""
import json
from pathlib import Path
import telegram_news_compat as news

directory = Path('feed')
directory.mkdir(exist_ok=True)
expected = {p['id'] for p in news.parse_posts(news.fetch(news.FEED_URL, 2_000_000).decode('utf-8')) if p['source_photo']}
news.sync(directory)
posts = json.loads((directory / 'latest.json').read_text(encoding='utf-8'))['posts']
for post in posts:
    if post['id'] in expected and (not post.get('photo') or not (directory / post['photo']).is_file()):
        raise RuntimeError('Photo missing for post %s; retaining previous published feed' % post['id'])

