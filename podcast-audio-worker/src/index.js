const RSS_HEADERS = {
  'Content-Type': 'application/rss+xml; charset=utf-8',
  // Apple can cache aggressively; keep the edge/browser cache short so new briefings appear quickly.
  'Cache-Control': 'public, max-age=60',
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';

    if (path === '/feed.xml') {
      const object = await env.AUDIO_BUCKET.get('feed.xml');
      if (!object) return new Response('Podcast feed not found', { status: 404 });
      const headers = new Headers(RSS_HEADERS);
      headers.set('ETag', object.httpEtag);
      headers.set('Content-Length', String(object.size));
      return new Response(request.method === 'HEAD' ? null : object.body, { status: 200, headers });
    }

    if (path === '/rss.xml' || path === '/podcast.xml') {
      url.pathname = '/feed.xml';
      url.search = '';
      return Response.redirect(url.toString(), 301);
    }

    if (path === '/podcast-cover.jpg') {
      const key = 'podcast-cover.jpg';
      const rangeHeader = request.headers.get('Range');
      const head = request.method === 'HEAD';

      let range;
      let status = 200;
      if (rangeHeader) {
        const rangeMatch = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader);
        if (!rangeMatch) return new Response('Invalid Range', { status: 416 });
        const startText = rangeMatch[1];
        const endText = rangeMatch[2];
        const meta = await env.AUDIO_BUCKET.head(key);
        if (!meta) return new Response('Podcast artwork not found', { status: 404 });

        const size = meta.size;
        let start;
        let end;
        if (startText === '') {
          const suffixLength = Number(endText);
          start = Math.max(size - suffixLength, 0);
          end = size - 1;
        } else {
          start = Number(startText);
          end = endText ? Number(endText) : size - 1;
        }
        if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start || start >= size) {
          return new Response('Range Not Satisfiable', {
            status: 416,
            headers: { 'Content-Range': `bytes */${size}` },
          });
        }
        end = Math.min(end, size - 1);
        range = { offset: start, length: end - start + 1 };
        status = 206;
      }

      const object = await env.AUDIO_BUCKET.get(key, range ? { range } : undefined);
      if (!object) return new Response('Podcast artwork not found', { status: 404 });

      const headers = new Headers();
      object.writeHttpMetadata(headers);
      headers.set('Content-Type', 'image/jpeg');
      headers.set('Accept-Ranges', 'bytes');
      headers.set('ETag', object.httpEtag);
      headers.set('Cache-Control', 'public, max-age=86400');
      if (range && object.range) {
        headers.set('Content-Range', `bytes ${object.range.offset}-${object.range.offset + object.range.length - 1}/${object.size}`);
        headers.set('Content-Length', String(object.range.length));
      } else {
        headers.set('Content-Length', String(object.size));
      }
      return new Response(head ? null : object.body, { status, headers });
    }

    if (path === '/') {
      url.pathname = '/feed.xml';
      return Response.redirect(url.toString(), 302);
    }

    const match = /^\/audio\/([^/?#]+)\.(mp3|wav)$/i.exec(path);
    if (!match) {
      return new Response('Not found', { status: 404 });
    }
    if (match[2].toLowerCase() === 'wav') {
      url.pathname = `/audio/${match[1]}.mp3`;
      return Response.redirect(url.toString(), 301);
    }

    const key = `audio/${match[1]}.mp3`;
    const rangeHeader = request.headers.get('Range');
    const head = request.method === 'HEAD';

    let range;
    let status = 200;
    if (rangeHeader) {
      const rangeMatch = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader);
      if (!rangeMatch) return new Response('Invalid Range', { status: 416 });
      const startText = rangeMatch[1];
      const endText = rangeMatch[2];
      const meta = await env.AUDIO_BUCKET.head(key);
      if (!meta) return new Response('Not found', { status: 404 });

      const size = meta.size;
      let start;
      let end;
      if (startText === '') {
        const suffixLength = Number(endText);
        start = Math.max(size - suffixLength, 0);
        end = size - 1;
      } else {
        start = Number(startText);
        end = endText ? Number(endText) : size - 1;
      }
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start || start >= size) {
        return new Response('Range Not Satisfiable', {
          status: 416,
          headers: { 'Content-Range': `bytes */${size}` },
        });
      }
      end = Math.min(end, size - 1);
      range = { offset: start, length: end - start + 1 };
      status = 206;
    }

    const object = await env.AUDIO_BUCKET.get(key, range ? { range } : undefined);
    if (!object) return new Response('Not found', { status: 404 });

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Content-Type', 'audio/mpeg');
    headers.set('Accept-Ranges', 'bytes');
    headers.set('ETag', object.httpEtag);
    headers.set('Cache-Control', 'public, max-age=31536000, immutable');

    if (range && object.range) {
      headers.set('Content-Range', `bytes ${object.range.offset}-${object.range.offset + object.range.length - 1}/${object.size}`);
      headers.set('Content-Length', String(object.range.length));
    } else {
      headers.set('Content-Length', String(object.size));
    }

    return new Response(head ? null : object.body, { status, headers });
  },
};

