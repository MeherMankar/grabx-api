export default {
  async fetch(request, env, ctx) {
    return new Response("Worker is alive", {
      status: 200,
      headers: { "Content-Type": "text/plain" }
    });
  }
};
