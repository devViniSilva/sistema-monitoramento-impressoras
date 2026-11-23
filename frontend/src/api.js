async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
  if (response.status === 401) { const error = new Error('Sua sessão expirou ou é necessário entrar.'); error.status = 401; throw error; }
  if (!response.ok) { let message = `Erro HTTP ${response.status}`; try { message = (await response.json()).erro || message; } catch {} const error = new Error(message); error.status = response.status; throw error; }
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response.text();
}
export const api = {
  me: () => request('/api/me'),
  printers: () => request('/api/printers'),
  login: (usuario, senha) => request('/api/login', { method: 'POST', body: JSON.stringify({ usuario, senha }) }),
  logout: () => request('/api/logout', { method: 'POST' }),
  save: (printer) => request(`/api/printers/${encodeURIComponent(printer.id)}`, { method: 'PUT', body: JSON.stringify(printer) }),
  remove: (id) => request(`/api/printers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  collect: () => request('/api/coletar', { method: 'POST' })
};
