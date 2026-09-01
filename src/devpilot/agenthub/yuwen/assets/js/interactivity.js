/* 小学语文课件 · 互动逻辑：翻页 / 点读 / 答案显隐 / 计时器 / HanziWriter */
(function () {
  'use strict';
  var slides = [];
  var idx = 0;

  function init() {
    slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
    if (!slides.length) return;
    show(0);
    bindKeys();
    bindNav();
    bindReveal();
    bindReadAloud();
    bindHanziWriter();
    bindTimer();
  }

  function show(n) {
    idx = Math.max(0, Math.min(slides.length - 1, n));
    slides.forEach(function (s, i) {
      s.classList.toggle('active', i === idx);
    });
    var counter = document.querySelector('.nav .counter');
    if (counter) counter.textContent = (idx + 1) + ' / ' + slides.length;
    var prog = document.querySelector('.progress');
    if (prog) prog.style.width = ((idx + 1) / slides.length * 100) + '%';
    updateNavBtns();
  }

  function next() { show(idx + 1); }
  function prev() { show(idx - 1); }

  function updateNavBtns() {
    var prevBtn = document.querySelector('.nav .prev');
    var nextBtn = document.querySelector('.nav .next');
    if (prevBtn) prevBtn.disabled = (idx === 0);
    if (nextBtn) nextBtn.disabled = (idx === slides.length - 1);
  }

  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') { e.preventDefault(); next(); }
      else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); prev(); }
      else if (e.key === 'Home') show(0);
      else if (e.key === 'End') show(slides.length - 1);
      else if (e.key === 'f' || e.key === 'F') toggleFullscreen();
    });
  }

  function bindNav() {
    var prevBtn = document.querySelector('.nav .prev');
    var nextBtn = document.querySelector('.nav .next');
    if (prevBtn) prevBtn.addEventListener('click', prev);
    if (nextBtn) nextBtn.addEventListener('click', next);
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
    } else {
      document.exitFullscreen && document.exitFullscreen();
    }
  }

  // 答案显隐：点 .reveal 块切换隐藏
  function bindReveal() {
    document.querySelectorAll('.reveal').forEach(function (el) {
      el.style.cursor = 'pointer';
      var hidden = el.querySelector('.reveal-hidden');
      if (hidden) hidden.style.display = 'none';
      el.addEventListener('click', function () {
        if (hidden) {
          hidden.style.display = (hidden.style.display === 'none') ? 'block' : 'none';
        }
      });
    });
  }

  // 点读：点汉字用 SpeechSynthesis 朗读（浏览器 TTS，需中文语音）
  function bindReadAloud() {
    if (!('speechSynthesis' in window)) return;
    document.querySelectorAll('[data-read]').forEach(function (el) {
      el.style.cursor = 'pointer';
      el.title = '点击朗读';
      el.addEventListener('click', function () {
        var text = el.getAttribute('data-read') || el.textContent;
        var u = new SpeechSynthesisUtterance(text);
        u.lang = 'zh-CN';
        u.rate = 0.85;
        speechSynthesis.cancel();
        speechSynthesis.speak(u);
      });
    });
  }

  // HanziWriter 笔顺动画：含 data-hanzi 的元素加载后绘制
  function bindHanziWriter() {
    if (typeof HanziWriter === 'undefined') {
      // CDN 未就绪则惰性加载
      var s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/hanzi-writer@3.5/dist/hanzi-writer.min.js';
      s.onload = drawAllWriters;
      document.head.appendChild(s);
    } else {
      drawAllWriters();
    }
  }

  var writers = [];
  function drawAllWriters() {
    document.querySelectorAll('[data-hanzi]').forEach(function (el) {
      var ch = el.getAttribute('data-hanzi');
      el.innerHTML = '';
      try {
        var w = HanziWriter.create(el, ch, {
          width: 120, height: 120, padding: 4,
          strokeColor: '#3D2B1F', radicalColor: '#E8743C',
          drawingWidth: 24, showOutline: true,
          delayBetweenStrokes: 200
        });
        el.style.cursor = 'pointer';
        el.addEventListener('click', function () { w.animateCharacter(); });
        writers.push(w);
      } catch (e) {
        el.textContent = ch; // 字库无此字则退化
      }
    });
  }

  // 计时器：含 .timer 的按钮
  function bindTimer() {
    document.querySelectorAll('.timer').forEach(function (btn) {
      var seconds = parseInt(btn.getAttribute('data-seconds') || '60', 10);
      var timer = null;
      btn.addEventListener('click', function () {
        if (timer) { clearInterval(timer); timer = null; btn.textContent = '⏱ ' + seconds + 's'; return; }
        var left = seconds;
        btn.textContent = '⏱ ' + left + 's';
        timer = setInterval(function () {
          left--;
          btn.textContent = '⏱ ' + left + 's';
          if (left <= 0) { clearInterval(timer); timer = null; btn.textContent = '⏱ 时间到！'; }
        }, 1000);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
