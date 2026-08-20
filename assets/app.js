const releaseTabs = document.getElementById("release-tabs");
const releaseTitle = document.getElementById("release-title");
const toolchainTabs = document.getElementById("toolchain-tabs");
const targetList = document.getElementById("target-list");
const baseTabs = document.getElementById("base-tabs");
const profileTabs = document.getElementById("profile-tabs");
const profileCopy = document.getElementById("profile-copy");
const imageList = document.getElementById("image-list");
const catalogStatus = document.getElementById("catalog-status");

let catalog;
const state = { release: null, toolchain: null, stream: null, profile: null };

function humanize(value) {
  return value
    .split("-")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function baseLabel(base) {
  return `${humanize(base.distro)} ${base.os_version}`;
}

function selectedRelease() {
  return catalog.releases.find((release) => release.version === state.release);
}

function selectedToolchain() {
  return selectedRelease().toolchains.find((toolchain) => toolchain.version === state.toolchain);
}

function selectedTarget() {
  return selectedToolchain().targets.find((target) => target.stream_id === state.stream);
}

function selectedProfile(target = selectedTarget()) {
  return target.profiles.find((profile) => profile.name === state.profile);
}

function preferredToolchain(release) {
  return release.toolchains.find((toolchain) => toolchain.targets.some((target) => target.aliases.length > 0)) ?? release.toolchains[0];
}

function preferredTarget(toolchain) {
  return toolchain.targets.find((target) => target.aliases.length > 0) ?? toolchain.targets[0];
}

function selectRelease(version) {
  state.release = version;
  const toolchain = preferredToolchain(selectedRelease());
  state.toolchain = toolchain.version;
  const target = preferredTarget(toolchain);
  state.stream = target.stream_id;
  state.profile = target.profiles.some((profile) => profile.name === "core") ? "core" : target.profiles[0].name;
}

function selectToolchain(version) {
  state.toolchain = version;
  const target = preferredTarget(selectedToolchain());
  state.stream = target.stream_id;
  state.profile = target.profiles.some((profile) => profile.name === state.profile) ? state.profile : target.profiles[0].name;
}

function createElement(name, className, text) {
  const element = document.createElement(name);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function architectureCell(architecture) {
  const available = architecture.status === "published";
  const cell = createElement(
    "td",
    `architecture-status ${available ? "available" : "unavailable"}`,
    available ? "✅" : "❌",
  );
  cell.setAttribute(
    "aria-label",
    available ? "Architecture published" : "Architecture not published",
  );
  return cell;
}

function profileArchitectureStatus(target, architecture) {
  const profile = selectedProfile(target);
  return profile.images.length > 0 && profile.images.every((image) => image.architectures[architecture].status === "published");
}

function architectureBadge(architecture, available) {
  return createElement("span", `arch ${available ? "available" : "unavailable"}`, `${available ? "✅" : "❌"} ${architecture.toUpperCase()}`);
}

function renderReleaseTabs() {
  releaseTabs.replaceChildren();
  for (const release of catalog.releases) {
    const button = createElement("button", `release-tab${state.release === release.version ? " active" : ""}`);
    button.type = "button";
    button.append(release.version, createElement("small", "", humanize(release.series)));
    button.addEventListener("click", () => {
      selectRelease(release.version);
      render();
    });
    releaseTabs.appendChild(button);
  }
}

function renderToolchains() {
  toolchainTabs.replaceChildren();
  for (const toolchain of selectedRelease().toolchains) {
    const button = createElement("button", `toolchain-tab${state.toolchain === toolchain.version ? " active" : ""}`, `Kolla ${toolchain.version}`);
    button.type = "button";
    button.addEventListener("click", () => {
      selectToolchain(toolchain.version);
      render();
    });
    toolchainTabs.appendChild(button);
  }
}

function renderTargets() {
  targetList.replaceChildren();
  for (const target of selectedToolchain().targets) {
    const row = document.createElement("tr");
    const name = createElement("td", "target-name", baseLabel(target.base));
    const tag = document.createElement("td");
    tag.appendChild(createElement("code", "", target.exact_tag));
    const architectures = document.createElement("td");
    architectures.append(
      architectureBadge("amd64", profileArchitectureStatus(target, "amd64")),
      architectureBadge("arm64", profileArchitectureStatus(target, "arm64")),
    );
    row.append(name, tag, architectures);
    targetList.appendChild(row);
  }
}

function renderBaseTabs() {
  baseTabs.replaceChildren();
  for (const target of selectedToolchain().targets) {
    const button = createElement("button", `base-tab${state.stream === target.stream_id ? " active" : ""}`, baseLabel(target.base));
    button.type = "button";
    button.addEventListener("click", () => {
      state.stream = target.stream_id;
      state.profile = target.profiles.some((profile) => profile.name === state.profile) ? state.profile : target.profiles[0].name;
      render();
    });
    baseTabs.appendChild(button);
  }
}

function renderProfiles() {
  profileTabs.replaceChildren();
  for (const profile of selectedTarget().profiles) {
    const button = createElement("button", `profile-tab${state.profile === profile.name ? " active" : ""}`);
    button.type = "button";
    button.append(humanize(profile.name), createElement("small", "", String(profile.image_count)));
    button.addEventListener("click", () => {
      state.profile = profile.name;
      render();
    });
    profileTabs.appendChild(button);
  }
}

function renderImages() {
  const target = selectedTarget();
  const profile = selectedProfile();
  const published = profile.images.filter((image) => Object.values(image.architectures).every((architecture) => architecture.status === "published")).length;
  profileCopy.textContent = `${published}/${profile.image_count} images · ${baseLabel(target.base)}`;
  imageList.replaceChildren();
  for (const image of profile.images) {
    const row = document.createElement("tr");
    row.append(
      createElement("td", "image-category", humanize(image.service_area)),
      createElement("td", "image-name", image.name),
      architectureCell(image.architectures.amd64),
      architectureCell(image.architectures.arm64),
    );
    imageList.appendChild(row);
  }
}

function renderStatus() {
  catalogStatus.replaceChildren(createElement("span", "status-dot", ""), document.createTextNode("Live GHCR catalog"));
}

function render() {
  const release = selectedRelease();
  releaseTitle.textContent = `${release.version} (${humanize(release.series)})`;
  renderReleaseTabs();
  renderToolchains();
  renderTargets();
  renderBaseTabs();
  renderProfiles();
  renderImages();
  renderStatus();
}

async function loadCatalog() {
  if (globalThis.IMAGE_CATALOG) return globalThis.IMAGE_CATALOG;
  const response = await fetch("catalog.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`catalog.json request failed: ${response.status}`);
  return response.json();
}

function renderError(error) {
  catalogStatus.replaceChildren(createElement("span", "status-dot error", ""), document.createTextNode("Catalog unavailable"));
  const message = createElement("p", "catalog-error", "Unable to load the generated catalog. Start a local web server or regenerate catalog.json.");
  message.title = error.message;
  document.querySelector(".catalog-content").prepend(message);
}

loadCatalog()
  .then((value) => {
    if (!value || !Array.isArray(value.releases) || value.releases.length === 0) {
      throw new Error("catalog contains no releases");
    }
    catalog = value;
    selectRelease(catalog.releases.find((release) => release.toolchains.some((toolchain) => toolchain.targets.some((target) => target.aliases.length > 0)))?.version ?? catalog.releases[0].version);
    render();
  })
  .catch(renderError);
