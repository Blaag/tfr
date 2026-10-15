export function isTranscriptEvent(event) {
  return !(event?.direction === "outbound" && event?.kind === "command");
}
