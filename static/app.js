document.addEventListener("DOMContentLoaded", () => {
  const dateInput = document.getElementById("q-date");
  const startInput = document.getElementById("q-start");
  const endInput = document.getElementById("q-end");

  if (dateInput) {
    dateInput.addEventListener("change", () => {
      const year = dateInput.value.slice(0, 4);
      if (!/^\d{4}$/.test(year)) return;
      if (startInput) startInput.value = `${year}-01-01`;
      if (endInput) endInput.value = `${year}-12-31`;
    });
  }
});
