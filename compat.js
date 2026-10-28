/* MIOT Panel compatibility layer for old Android WebView (4.0+). */
(function () {
  'use strict';

  if (!window.Promise) {
    (function () {
      function asap(fn) { setTimeout(fn, 0); }
      function Promise(executor) {
        if (!(this instanceof Promise)) { throw new TypeError('Promise constructor requires new'); }
        var self = this;
        self._state = 0;
        self._value = undefined;
        self._handlers = [];
        function flush() {
          if (!self._state) { return; }
          asap(function () {
            var handlers = self._handlers;
            self._handlers = [];
            for (var i = 0; i < handlers.length; i++) {
              (function (h) {
                var cb = self._state === 1 ? h.onFulfilled : h.onRejected;
                if (typeof cb !== 'function') {
                  (self._state === 1 ? h.resolve : h.reject)(self._value);
                  return;
                }
                try { h.resolve(cb(self._value)); } catch (e) { h.reject(e); }
              })(handlers[i]);
            }
          });
        }
        function settle(state, value) {
          if (self._state) { return; }
          if (state === 1 && value && typeof value.then === 'function') {
            value.then(resolve, reject);
            return;
          }
          self._state = state;
          self._value = value;
          flush();
        }
        function resolve(value) { settle(1, value); }
        function reject(reason) { settle(2, reason); }
        try { executor(resolve, reject); } catch (e) { reject(e); }
      }
      Promise.prototype.then = function (onFulfilled, onRejected) {
        var self = this;
        return new Promise(function (resolve, reject) {
          self._handlers.push({ onFulfilled: onFulfilled, onRejected: onRejected,
                                resolve: resolve, reject: reject });
          if (self._state) {
            var handlers = self._handlers;
            self._handlers = [];
            setTimeout(function () {
              for (var i = 0; i < handlers.length; i++) {
                (function (h) {
                  var cb = self._state === 1 ? h.onFulfilled : h.onRejected;
                  if (typeof cb !== 'function') {
                    (self._state === 1 ? h.resolve : h.reject)(self._value);
                    return;
                  }
                  try { h.resolve(cb(self._value)); } catch (e) { h.reject(e); }
                })(handlers[i]);
              }
            }, 0);
          }
        });
      };
      Promise.prototype['catch'] = function (fn) { return this.then(null, fn); };
      Promise.resolve = function (v) { return new Promise(function (r) { r(v); }); };
      Promise.reject = function (e) { return new Promise(function (r, j) { j(e); }); };
      window.Promise = Promise;
    })();
  }

  if (!window.fetch) {
    window.fetch = function (input, init) {
      init = init || {};
      return new window.Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        var method = (init.method || 'GET').toUpperCase();
        var headers = init.headers || {};
        try { xhr.open(method, input, true); } catch (e) { reject(e); return; }
        if (init.credentials === 'include' || init.credentials === 'same-origin') {
          try { xhr.withCredentials = true; } catch (e2) {}
        }
        for (var k in headers) {
          if (headers.hasOwnProperty(k)) {
            try { xhr.setRequestHeader(k, headers[k]); } catch (e3) {}
          }
        }
        xhr.onreadystatechange = function () {
          if (xhr.readyState !== 4) { return; }
          var body = xhr.responseText || '';
          resolve({
            ok: xhr.status >= 200 && xhr.status < 300,
            status: xhr.status,
            statusText: xhr.statusText || '',
            url: xhr.responseURL || String(input),
            headers: { get: function (name) { return xhr.getResponseHeader(name); } },
            text: function () { return window.Promise.resolve(body); },
            json: function () {
              return new window.Promise(function (res, rej) {
                try { res(JSON.parse(body || 'null')); } catch (e4) { rej(e4); }
              });
            }
          });
        };
        xhr.onerror = function () { reject(new Error('Network request failed')); };
        xhr.ontimeout = function () { reject(new Error('Network request timeout')); };
        try { xhr.send(init.body || null); } catch (e5) { reject(e5); }
      });
    };
  }

  if (!Array.from) {
    Array.from = function (obj) {
      var out = [];
      if (!obj) { return out; }
      if (typeof obj.length === 'number') {
        for (var i = 0; i < obj.length; i++) { out.push(obj[i]); }
        return out;
      }
      for (var k in obj) { if (obj.hasOwnProperty(k)) { out.push(obj[k]); } }
      return out;
    };
  }
  if (!Array.prototype.includes) {
    Array.prototype.includes = function (value) {
      for (var i = 0; i < this.length; i++) {
        if (this[i] === value || (value !== value && this[i] !== this[i])) { return true; }
      }
      return false;
    };
  }
  if (!Array.prototype.find) {
    Array.prototype.find = function (fn, thisArg) {
      for (var i = 0; i < this.length; i++) {
        if (fn.call(thisArg, this[i], i, this)) { return this[i]; }
      }
      return undefined;
    };
  }
  if (!String.prototype.startsWith) {
    String.prototype.startsWith = function (s, pos) { return this.substr(pos || 0, s.length) === s; };
  }
  if (!String.prototype.endsWith) {
    String.prototype.endsWith = function (s, len) {
      var str = String(this);
      var n = len === undefined ? str.length : len;
      return str.substring(n - s.length, n) === s;
    };
  }
  if (!String.prototype.includes) {
    String.prototype.includes = function (s, pos) { return this.indexOf(s, pos || 0) !== -1; };
  }
  if (!Object.assign) {
    Object.assign = function (target) {
      for (var i = 1; i < arguments.length; i++) {
        var src = arguments[i];
        if (!src) { continue; }
        for (var k in src) { if (src.hasOwnProperty(k)) { target[k] = src[k]; } }
      }
      return target;
    };
  }
  if (!window.URLSearchParams) {
    window.URLSearchParams = function (query) {
      this._data = [];
      query = String(query || '');
      if (query.charAt(0) === '?') { query = query.substring(1); }
      var parts = query.split('&');
      for (var i = 0; i < parts.length; i++) {
        if (!parts[i]) { continue; }
        var p = parts[i].split('=');
        this._data.push([decodeURIComponent(p[0] || ''),
                         decodeURIComponent((p[1] || '').replace(/\+/g, ' '))]);
      }
    };
    window.URLSearchParams.prototype.get = function (name) {
      for (var i = 0; i < this._data.length; i++) {
        if (this._data[i][0] === name) { return this._data[i][1]; }
      }
      return null;
    };
    window.URLSearchParams.prototype.append = function (name, value) {
      this._data.push([String(name), String(value)]);
    };
    window.URLSearchParams.prototype.toString = function () {
      var out = [];
      for (var i = 0; i < this._data.length; i++) {
        out.push(encodeURIComponent(this._data[i][0]) + '=' +
                 encodeURIComponent(this._data[i][1]));
      }
      return out.join('&');
    };
  }
  if (!window.requestAnimationFrame) {
    window.requestAnimationFrame = function (fn) {
      return setTimeout(function () { fn(Date.now ? Date.now() : new Date().getTime()); }, 16);
    };
    window.cancelAnimationFrame = function (id) { clearTimeout(id); };
  }

  var supportsGrid = false;
  try {
    supportsGrid = !!(window.CSS && CSS.supports &&
                      (CSS.supports('display', 'grid') || CSS.supports('display', '-ms-grid')));
  } catch (e6) {}
  if (!supportsGrid) {
    var root = document.documentElement;
    root.className = (root.className ? root.className + ' ' : '') + 'legacy-webview';
    var style = document.createElement('style');
    style.type = 'text/css';
    style.appendChild(document.createTextNode(
      'html.legacy-webview,html.legacy-webview body{background:#eef1f5!important;color:#1f2937!important;}' +
      'html.legacy-webview .card,html.legacy-webview .device,html.legacy-webview .tile,html.legacy-webview .panel{' +
      'background:#fff!important;color:#1f2937!important;border:1px solid #d7dde8!important;border-radius:10px!important;}' +
      'html.legacy-webview .grid,html.legacy-webview .cards,html.legacy-webview .devices,' +
      'html.legacy-webview .deviceGrid,html.legacy-webview .rooms{display:block!important;}' +
      'html.legacy-webview .device,html.legacy-webview .card,html.legacy-webview .tile{' +
      'display:block!important;width:auto!important;min-width:0!important;margin:8px 0!important;}' +
      'html.legacy-webview button,html.legacy-webview .btn{min-height:36px!important;}' +
      'html.legacy-webview input,html.legacy-webview select{min-height:32px!important;}'
    ));
    (document.head || root).appendChild(style);
  }
})();
