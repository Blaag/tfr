export function restoreTranscriptPosition({ transcript, reading, schedule, onRestore }) {
  const atLive = reading.atLive;
  const savedScrollTop = reading.scrollTop;
  const apply = () => {
    if (atLive) {
      transcript.scrollTo({ top: transcript.scrollHeight, behavior: "auto" });
      reading.atLive = true;
      reading.unseenLive = 0;
      reading.scrollTop = transcript.scrollTop;
    } else {
      transcript.scrollTop = savedScrollTop;
    }
    onRestore();
  };

  apply();
  schedule(apply);
}
