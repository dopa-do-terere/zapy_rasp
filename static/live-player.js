/*
 * LivePlayer: vídeo ao vivo do Cockpit por MSE via WebSocket (proxy do zapy pro go2rtc,
 * ver /ws/cockpit/face-terminals/<id>/live em app.py). Sem lib; só H.264 (sem áudio).
 *
 *   var p = new LivePlayer(videoEl, '/ws/cockpit/face-terminals/abc/live', {
 *       onStatus: function(state, detail) {}   // 'connecting' | 'live' | 'error'
 *   });
 *   p.start(); ... p.stop();
 *
 * Fica na borda ao vivo (pula se o atraso passa de 1,5 s), descarta buffer com mais de
 * 10 s, reconecta com backoff 1,2,4..30 s e fecha a conexão com a aba escondida.
 * Close codes 44xx (recusa do servidor) não reconectam, exceto 4429 (limite): 30 s.
 */
(function(global) {
    'use strict';

    var CODECS = 'avc1.640029,avc1.64002A,avc1.640033,avc1.4D401F,avc1.42E01E';
    var MAX_LAG_SECONDS = 1.5;
    var KEEP_BEHIND_SECONDS = 10;
    var MAX_BACKOFF_SECONDS = 30;
    var REFUSAL_MESSAGES = {
        4401: 'sessão expirada, entre de novo',
        4403: 'vídeo desligado',
        4404: 'terminal sem vídeo',
        4429: 'limite de visualizações'
    };

    function absoluteWsUrl(path) {
        if (/^wss?:\/\//.test(path)) return path;
        return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
    }

    function LivePlayer(video, wsUrl, opts) {
        this.video = video;
        this.wsUrl = absoluteWsUrl(wsUrl);
        this.onStatus = (opts && opts.onStatus) || function() {};
        this.stopped = true;
        this.refused = false;  // recusa 44xx definitiva: não reabre nem ao voltar pra aba
        this.retry = 0;
        this.lastError = null;
        this.ws = null;
        this.ms = null;
        this.sb = null;
        this.queue = [];
        this.objectUrl = null;
        this.tickTimer = null;
        this.reconnectTimer = null;
        this._onVisibility = this._visibility.bind(this);
        this._onPlaying = this._playing.bind(this);
    }

    LivePlayer.prototype.start = function() {
        if (!this.stopped) return;
        this.stopped = false;
        this.refused = false;
        document.addEventListener('visibilitychange', this._onVisibility);
        this.video.addEventListener('playing', this._onPlaying);
        if (!document.hidden) this._open();
    };

    LivePlayer.prototype.stop = function() {
        this.stopped = true;
        document.removeEventListener('visibilitychange', this._onVisibility);
        this.video.removeEventListener('playing', this._onPlaying);
        this._teardown();
    };

    LivePlayer.prototype._status = function(state, detail) {
        try { this.onStatus(state, detail); } catch (e) { /* callback do chamador */ }
    };

    LivePlayer.prototype._playing = function() {
        this.retry = 0;
        this.lastError = null;
        this._status('live');
    };

    LivePlayer.prototype._visibility = function() {
        if (this.stopped || this.refused) return;
        if (document.hidden) this._teardown();
        else if (!this.ws) this._open();
    };

    LivePlayer.prototype._teardown = function() {
        clearInterval(this.tickTimer);
        clearTimeout(this.reconnectTimer);
        this.tickTimer = null;
        this.reconnectTimer = null;
        if (this.ws) {
            this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
            try { this.ws.close(); } catch (e) { /* já fechado */ }
            this.ws = null;
        }
        this.sb = null;
        this.queue = [];
        this.ms = null;
        this.video.removeAttribute('src');
        try { this.video.load(); } catch (e) { /* sem mídia */ }
        if (this.objectUrl) {
            URL.revokeObjectURL(this.objectUrl);
            this.objectUrl = null;
        }
    };

    LivePlayer.prototype._open = function() {
        var self = this;
        this._teardown();
        if (!global.MediaSource) {
            this._status('error', 'navegador sem suporte a vídeo ao vivo (MSE)');
            return;
        }
        var ms = new MediaSource();
        this.ms = ms;
        this.objectUrl = URL.createObjectURL(ms);
        this.video.src = this.objectUrl;
        this._status(this.lastError ? 'error' : 'connecting', this.lastError);

        ms.addEventListener('sourceopen', function() {
            if (self.ms !== ms) return;
            var ws = new WebSocket(self.wsUrl);
            ws.binaryType = 'arraybuffer';
            self.ws = ws;
            ws.onopen = function() {
                ws.send(JSON.stringify({ type: 'mse', value: CODECS }));
            };
            ws.onmessage = function(ev) {
                if (self.ws !== ws) return;
                if (typeof ev.data === 'string') {
                    var msg;
                    try { msg = JSON.parse(ev.data); } catch (e) { return; }
                    if (msg.type === 'mse' && !self.sb) {
                        try {
                            self.sb = ms.addSourceBuffer(msg.value);
                            self.sb.mode = 'segments';
                            self.sb.addEventListener('updateend', function() { self._pump(); });
                        } catch (e) {
                            self.lastError = 'codec não suportado pelo navegador';
                            self._status('error', self.lastError);
                            try { ws.close(); } catch (e2) { /* já fechado */ }
                        }
                    } else if (msg.type === 'error') {
                        self.lastError = msg.value;
                        self._status('error', msg.value);
                    }
                    return;
                }
                self.queue.push(ev.data);
                self._pump();
            };
            ws.onclose = function(ev) {
                if (self.stopped || self.ws !== ws) return;
                self.ws = null;
                self._reconnect(ev.code);
            };
        }, { once: true });

        this.tickTimer = setInterval(function() { self._tick(); }, 500);
    };

    LivePlayer.prototype._pump = function() {
        var sb = this.sb;
        if (!sb || sb.updating || !this.queue.length) return;
        try {
            sb.appendBuffer(this.queue.shift());
        } catch (e) {
            // buffer cheio ou SourceBuffer inválido: recomeça a conexão do zero
            if (this.ws) { try { this.ws.close(); } catch (e2) { /* já fechado */ } }
        }
    };

    LivePlayer.prototype._tick = function() {
        var video = this.video, sb = this.sb;
        if (!sb) return;
        try {
            var b = video.buffered;
            if (!b.length) return;
            var end = b.end(b.length - 1);
            if (end - video.currentTime > MAX_LAG_SECONDS) video.currentTime = end - 0.1;
            if (!sb.updating && video.currentTime - b.start(0) > KEEP_BEHIND_SECONDS) {
                sb.remove(b.start(0), video.currentTime - KEEP_BEHIND_SECONDS);
            }
            if (video.paused) {
                var p = video.play();
                if (p && p.catch) p.catch(function() { /* autoplay bloqueado */ });
            }
        } catch (e) { /* SourceBuffer removido no meio do tick */ }
    };

    LivePlayer.prototype._reconnect = function(code) {
        var self = this;
        var delay;
        if (code >= 4400 && code < 4500) {
            var msg = REFUSAL_MESSAGES[code] || 'vídeo recusado pelo servidor';
            this._teardown();
            this._status('error', msg);
            if (code !== 4429) {
                this.refused = true;
                return;
            }
            this.lastError = msg;
            delay = 30;
        } else {
            delay = Math.min(MAX_BACKOFF_SECONDS, Math.pow(2, this.retry));
            this.retry += 1;
            this._teardown();
            this._status(this.lastError ? 'error' : 'connecting', this.lastError);
        }
        this.reconnectTimer = setTimeout(function() {
            if (!self.stopped && !document.hidden) self._open();
        }, delay * 1000);
    };

    global.LivePlayer = LivePlayer;
})(window);
