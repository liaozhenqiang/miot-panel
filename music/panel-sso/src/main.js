async function onInit() {
  console.log("[panel-sso] ready");
}

async function onDeinit() {
  console.log("[panel-sso] stopped");
}

;(globalThis).onInit = onInit;
;(globalThis).onDeinit = onDeinit;
