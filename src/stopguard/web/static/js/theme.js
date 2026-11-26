/* Setzt das Farbschema vor dem ersten Zeichnen (kein Aufblitzen). Klassisches Skript, kein Modul. */
(function () {
  var pref = null;
  try { pref = localStorage.getItem("sg-theme"); } catch (e) { /* privater Modus */ }
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function apply() {
    var theme = pref === "light" || pref === "dark" ? pref : (media && media.matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  }
  apply();
  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (pref !== "light" && pref !== "dark") { apply(); document.dispatchEvent(new CustomEvent("themechange")); }
    });
  }
  window.sgTheme = {
    get: function () { return pref === "light" || pref === "dark" ? pref : "system"; },
    set: function (value) {
      pref = value;
      try {
        if (value === "system") localStorage.removeItem("sg-theme"); else localStorage.setItem("sg-theme", value);
      } catch (e) { /* ignorieren */ }
      apply();
      document.dispatchEvent(new CustomEvent("themechange"));
    }
  };
})();
