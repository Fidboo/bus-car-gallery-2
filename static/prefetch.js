// Starts downloading a photo a moment before it is opened.
// On desktop: when the mouse rests on a thumbnail for ~120 ms. On touch
// screens: the moment the finger lands. The photo files are cached for a
// year, so whatever is fetched here is reused instantly by the photo page.
(function () {
    var c = navigator.connection || {};
    if (c.saveData || /(^|-)2g$/.test(c.effectiveType || '')) return;   // respect data saver / slow links

    var started = {};
    var timer = null;

    function cardOf(el) {
        return el && el.closest ? el.closest('a.card[data-full]') : null;
    }
    function warm(card) {
        var url = card && card.getAttribute('data-full');
        if (!url || started[url]) return;
        started[url] = true;
        var l = document.createElement('link');
        l.rel = 'prefetch';
        l.as = 'image';
        l.href = url;
        document.head.appendChild(l);
    }

    document.addEventListener('mouseover', function (e) {
        var card = cardOf(e.target);
        clearTimeout(timer);
        if (card) timer = setTimeout(function () { warm(card); }, 120);
    });
    document.addEventListener('mouseout', function () { clearTimeout(timer); });
    document.addEventListener('touchstart', function (e) { warm(cardOf(e.target)); },
                              { passive: true });
})();
