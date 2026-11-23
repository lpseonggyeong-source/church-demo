"use strict";

const openButton = document.getElementById("lp-open");
const feedback = document.getElementById("lp-status");

if (openButton && feedback) {
  const form = openButton.form;
  if (typeof window.paynix?.pay !== "function") {
    feedback.textContent = "결제 모듈을 불러오지 못했습니다. 네트워크를 확인하고 페이지를 새로고침해 주세요.";
  } else {
    openButton.disabled = false;
    feedback.textContent = "테스트 결제창을 열 수 있습니다.";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (openButton.disabled) return;
      openButton.disabled = true;
      feedback.textContent = "결제창을 준비하고 있습니다.";
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new URLSearchParams(new FormData(form)),
        });
        if (!response.ok) {
          feedback.textContent = response.status === 409
            ? "이미 결제를 시작한 거래입니다. 거래 상태를 확인해 주세요."
            : "결제창을 준비하지 못했습니다. 거래 상태를 확인해 주세요.";
          return;
        }
        const payload = await response.json();
        payload.responseFunction = (responseBody) => {
          let resultCode;
          try {
            resultCode = (typeof responseBody === "string" ? JSON.parse(responseBody) : responseBody)?.result?.resultCd;
          } catch (_) {
            resultCode = undefined;
          }
          feedback.textContent = resultCode === "0000"
            ? "PG 결제창에서 성공 응답을 받았습니다. 서버에서 승인 여부를 확인하기 전까지 처리 중으로 표시됩니다."
            : "결제창이 응답했습니다. 결제 여부는 PG 테스트 가맹점 내역에서 확인해 주세요.";
        };
        feedback.textContent = "결제창에서 카드 인증을 진행해 주세요.";
        window.paynix.pay(payload);
      } catch (_) {
        feedback.textContent = "결제창 연결을 확인할 수 없습니다. 다시 결제하기 전에 거래 상태를 확인해 주세요.";
      }
    });
  }
}
