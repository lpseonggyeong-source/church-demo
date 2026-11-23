"use strict";
// 시연판 전용: 장 페이지 하나로 로컬 서버의 '절 선택' 화면을 똑같이 재현합니다.
// 서버 app.py chapter_text()와 같은 표시: aria-current="location" + <span class="sr">선택한 절 </span> + 'N절 선택됨' 문구
(function () {
  var m = /^#verse-(\d+)$/.exec(location.hash);
  if (!m) return;
  var target = document.getElementById("verse-" + m[1]);
  if (!target || target.hasAttribute("aria-current")) return;
  document.querySelectorAll(".verses li[aria-current]").forEach(function (li) {
    var sr = li.firstElementChild;
    if (sr && sr.className === "sr") sr.remove();
    li.outerHTML = li.outerHTML.replace(' aria-current="location"', "");
  });
  // 서버와 같은 속성 순서: id → aria-current → tabindex, 맨 앞에 낭독기용 '선택한 절'
  target.outerHTML = target.outerHTML.replace(/^<li id="(verse-\d+)" tabindex="-1">/,
    '<li id="$1" aria-current="location" tabindex="-1"><span class="sr">선택한 절 </span>');
  document.querySelectorAll("span").forEach(function (span) {
    if (/^\d+절 선택됨 · 앞 절은 위로 스크롤$/.test(span.textContent)) span.textContent = m[1] + "절 선택됨 · 앞 절은 위로 스크롤";
  });
})();
