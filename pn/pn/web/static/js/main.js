// Entry point: registers every page route (pn/web/app.py PAGES) and starts the router.
import { route, startRouter } from "./core.js";
import { homeView, playersView } from "./views/players.js";
import { profileView } from "./views/profile.js";
import { reviewPickView, reviewView } from "./views/review.js";
import { handsView, handView } from "./views/hands.js";
import { chartsView } from "./views/charts.js";
import { strategyBrowserView } from "./views/strategy.js";
import { strategyNewView } from "./views/strategy_new.js";
import { strategyLibraryView } from "./views/strategy_library.js";
import { studyView } from "./views/study.js";
import { trainView } from "./views/train.js";
import { performanceView } from "./views/performance.js";

route("/", homeView, "home");
route("/players", playersView, "players");
route("/players/:pid", profileView, "players");
route("/review", reviewPickView, "review");
route("/review/:hero", reviewView, "review");
route("/hands", handsView, "hands");
route("/hands/:hand_id", handView, "hands");
route("/charts", chartsView, "charts"); // unlinked: the trainer drawer shows these grids
route("/train", trainView, "train");
route("/train/performance", performanceView, "train");
route("/strategy", studyView, "strategy");
route("/strategy/library", strategyLibraryView, "strategy"); // before /strategy/:name (first match wins)
route("/strategy/new", strategyNewView, "strategy");
route("/strategy/:name", strategyBrowserView, "strategy");

// Phone: the nav sits behind the menu button (components.css hides it at <= 700px).
const bar = document.querySelector(".topbar");
const menu = bar.querySelector(".menu-btn");
const setMenu = (open) => { bar.classList.toggle("nav-open", open); menu.setAttribute("aria-expanded", String(open)); };
menu.addEventListener("click", () => setMenu(!bar.classList.contains("nav-open")));
bar.querySelector("nav").addEventListener("click", (e) => { if (e.target.closest("a")) setMenu(false); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && bar.classList.contains("nav-open")) { setMenu(false); menu.focus(); }
});

startRouter();
