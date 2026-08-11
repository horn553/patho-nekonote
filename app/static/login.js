const params = new URLSearchParams(window.location.search);
if (params.has("error")) {
  document.querySelector("#login-error").hidden = false;
}
