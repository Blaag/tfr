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
