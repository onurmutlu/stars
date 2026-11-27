const files = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["index.html", "text/html; charset=utf-8"]],
  ["/stars-checkout.js", ["stars-checkout.js", "application/javascript; charset=utf-8"]],
  ["/stars-checkout.json", ["stars-checkout.json", "application/json; charset=utf-8"]]
]);

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const file = files.get(url.pathname);
    if (!file || !["GET", "HEAD"].includes(request.method)) {
      if (url.hostname === "stars.siyahkare.com") return fetch(request);
      return new Response("Not found", {status: 404});
    }
    const source = "https://raw.githubusercontent.com/onurmutlu/stars/gh-pages/" + file[0];
    const upstream = await fetch(source, {cf: {cacheEverything: true, cacheTtlByStatus: {"200-299": 60, "400-599": 0}}});
    if (!upstream.ok) return new Response("Page temporarily unavailable", {status: 503});
    const headers = {"Content-Type": file[1], "Cache-Control": "public, max-age=60", "X-Content-Type-Options": "nosniff"};
    if (request.method === "HEAD") return new Response(null, {headers});
    const page = new Response(upstream.body, {headers});
    if (file[0] !== "index.html") return page;
    return new HTMLRewriter().on("head", {
      element(head) {
        head.prepend("<script>if(new URLSearchParams(location.hash.slice(1)).has('tgWebAppData'))location.replace('/static/index.html'+location.search+location.hash)</script>", {html:true});
      }
    }).transform(page);
  }
};
