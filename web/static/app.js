// Small progressive enhancements; every page works without them.
document.documentElement.classList.add("js");

document.addEventListener("DOMContentLoaded", () => {
  // Ask before destructive or recording actions.
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Save a checkbox or select change straight away.
  document.querySelectorAll("form.auto-submit").forEach((form) => {
    form.addEventListener("change", () => form.submit());
  });

  // Follow a generation run, and show its results when it ends.
  const box = document.querySelector("[data-job-status]");
  if (!box) return;
  const field = (name) => box.querySelector(`[data-field="${name}"]`);
  const bar = box.querySelector("progress");
  const poll = async () => {
    try {
      const response = await fetch(box.dataset.jobStatus, { cache: "no-store" });
      const job = await response.json();
      if (job.status !== "running") {
        window.location.reload();
        return;
      }
      field("stage").textContent = job.stage;
      field("elapsed").textContent = job.elapsed;
      if (job.total) {
        bar.max = job.total;
        bar.value = job.done;
        field("count").textContent = `${job.done} of ${job.total}`;
      }
    } catch (error) {
      // The server may be restarting; try again on the next tick.
    }
    window.setTimeout(poll, 2000);
  };
  window.setTimeout(poll, 2000);
});
