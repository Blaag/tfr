export function submitPairing(document, code) {
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/pair";
  form.hidden = true;

  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "code";
  input.value = code;
  form.append(input);

  document.body.append(form);
  form.submit();
}

export function pairingCodeFromLink(value, expectedOrigin) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (
    url.origin !== expectedOrigin ||
    url.pathname !== "/" ||
    url.search ||
    url.username ||
    url.password
  ) {
    return null;
  }
  const parameters = new URLSearchParams(url.hash.slice(1));
  const entries = [...parameters.entries()];
  if (entries.length !== 1 || entries[0][0] !== "pair") return null;
  const code = entries[0][1];
  return code && code.length <= 128 && /^[\x21-\x7e]+$/.test(code) ? code : null;
}

export function createPairingSubmission(submit, schedule) {
  let pending = false;
  let generation = 0;

  return {
    start(code, delay = 0) {
      if (pending) return false;
      pending = true;
      generation += 1;
      const submissionGeneration = generation;
      if (delay > 0) {
        schedule(() => {
          if (pending && generation === submissionGeneration) submit(code);
        }, delay);
      } else submit(code);
      return true;
    },
    reset() {
      pending = false;
      generation += 1;
    },
  };
}
