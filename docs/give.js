"use strict";
// 온길 화면 보조 스크립트 — 없어도 헌금은 되고, 있으면 입력이 편해집니다.
// 1) 금액 빠른 입력(+1만 · +5만 · +10만 · 지우기)  2) 금액 쉼표 · 한글 표시  3) 조회 코드 복사

// app.py의 korean_amount()와 같은 결과를 내야 합니다. 50000 → "오만 원"
function koreanAmount(value) {
  if (!value || value <= 0) return "";
  const digits = "영일이삼사오육칠팔구";
  const groups = [];
  for (const unit of ["", "만", "억", "조"]) {
    const part = value % 10000;
    value = Math.floor(value / 10000);
    if (part) {
      let text = "";
      for (const [place, name] of [[1000, "천"], [100, "백"], [10, "십"], [1, ""]]) {
        const d = Math.floor(part / place) % 10;
        if (d) text += (d === 1 && name ? "" : digits[d]) + name;
      }
      groups.push(text + unit);
    }
    if (!value) break;
  }
  return groups.reverse().join(" ") + " 원";
}

const amountInput = document.querySelector("[data-amount]");
const amountWords = document.querySelector("[data-amount-words]");

function readAmount() {
  return parseInt((amountInput.value || "").replace(/[^0-9]/g, ""), 10) || 0;
}

// PC 웹 헌금 요약 카드 (있을 때만)
const sumAmount = document.querySelector("[data-sum-amount]");
const sumCategory = document.querySelector("[data-sum-category]");
const sumName = document.querySelector("[data-sum-name]");

function showAmount(value) {
  amountInput.value = value ? value.toLocaleString("ko-KR") : "";
  if (amountWords) amountWords.textContent = koreanAmount(value) || amountWords.dataset.empty || "";
  if (sumAmount) sumAmount.textContent = (value || 0).toLocaleString("ko-KR");
}

for (const radio of document.querySelectorAll('input[name="category"]')) {
  radio.addEventListener("change", () => { if (sumCategory) sumCategory.textContent = radio.parentElement.textContent.trim(); });
}
const nameInput = document.querySelector('input[name="name"]');
if (nameInput && sumName) nameInput.addEventListener("input", () => { sumName.textContent = nameInput.value.trim() || "익명"; });

if (amountInput) {
  amountInput.addEventListener("input", () => showAmount(Math.min(readAmount(), 100000000)));
  const quick = document.querySelector("[data-quick]");
  if (quick) {
    quick.hidden = false;
    quick.addEventListener("click", (event) => {
      const button = event.target.closest("[data-add]");
      if (!button) return;
      const add = parseInt(button.dataset.add, 10);
      showAmount(add ? Math.min(readAmount() + add, 100000000) : 0);
      amountInput.focus();
    });
  }
}

for (const button of document.querySelectorAll("[data-copy]")) {
  const target = document.getElementById(button.dataset.copy);
  if (!target || !navigator.clipboard) continue;
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      button.textContent = "복사됨";
    } catch (_) {
      button.textContent = "직접 선택해 복사";
    }
    setTimeout(() => { button.textContent = "복사"; }, 2000);
  });
}

// 관리자: 헌금 항목 줄은 고치면 바로 저장 (사용 체크 · 이름/설명 칸을 벗어날 때)
for (const form of document.querySelectorAll("form[data-autosave]")) {
  form.addEventListener("change", () => (form.requestSubmit ? form.requestSubmit() : form.submit()));
}
// 관리자: 교회 정보 → 성도 화면 미리보기
for (const input of document.querySelectorAll("[data-preview]")) {
  const target = document.querySelector(`[data-pv="${input.dataset.preview}"]`);
  if (!target) continue;
  input.addEventListener("input", () => { target.textContent = input.value; target.hidden = !input.value; });
}
