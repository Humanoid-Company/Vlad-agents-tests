// Address of the backend on Render. Set it after the first Render deploy, then redeploy on Vercel.
// For a one-off test you can also open the page with ?backend=https://your-service.onrender.com
window.APP_CONFIG = {
  BACKEND_URL: "https://vlad-agents-tests.onrender.com",
  // GPT-Live bills every open second. A pause closes the session after this many seconds
  // (not while a tool still runs); silence this long during a call pauses her.
  PAUSE_CLOSE_SECONDS: 15,
  IDLE_PAUSE_SECONDS: 90,
  // Approximate GPT-Live price per open minute, for the call meter under the status line.
  PRICE_PER_MINUTE_USD: 0.05,
};
