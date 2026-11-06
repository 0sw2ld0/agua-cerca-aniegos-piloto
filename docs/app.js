'use strict';
/* Agua Cerca web. Read-only: every request is a GET /query or a tile image.
   The cortes layer accepts public writes upstream: its text is untrusted and rendered with textContent only. */

const GEOSUNASS = 'https://geosunass.sunass.gob.pe/gisserver/rest/services/Hosted/';
const CAPA_CORTES = 'service_f5fafc1370944420ba18d9e682123812_form';
const RAW = 'https://raw.githubusercontent.com/0sw2ld0/agua-cerca-aniegos-piloto/main/data/';
const MINAM = 'https://geoservidorperu.minam.gob.pe/arcgis/rest/services/';
const LIMA = 'America/Lima';

/* ---------- utilidades ---------- */
const $ = id => document.getElementById(id);
function el(tag, attrs = {}, ...hijos) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const h of hijos) if (h != null && h !== false) n.append(h);
  return n;
}
const limpio = (t, max = 600) => String(t ?? '').replace(/\s+/g, ' ').trim().slice(0, max);
const titulo = t => limpio(t).toLocaleLowerCase('es-PE').replace(/(^|[\s(/-])(\p{L})/gu, (m, a, b) => a + b.toLocaleUpperCase('es-PE'));
const capital = t => { const s = limpio(t).toLocaleLowerCase('es-PE'); return s.charAt(0).toLocaleUpperCase('es-PE') + s.slice(1); };
const plano = t => String(t ?? '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
function enlaceSeguro(url, texto, hosts) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:' || (hosts && !hosts.includes(u.hostname))) return null;
    return el('a', { href: u.href, target: '_blank', rel: 'noopener noreferrer', text: texto || u.hostname.replace(/^www\./, '') });
  } catch { return null; }
}
const guardar = (k, v) => { try { localStorage.setItem(k, JSON.stringify({ fecha: Date.now(), valor: v })); } catch {} };
const leer = k => { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } };

const fmtDia = new Intl.DateTimeFormat('es-PE', { timeZone: LIMA, weekday: 'short', day: 'numeric', month: 'short' });
const fmtHora = new Intl.DateTimeFormat('es-PE', { timeZone: LIMA, hour: '2-digit', minute: '2-digit', hour12: false });
const fmtClave = new Intl.DateTimeFormat('en-CA', { timeZone: LIMA, year: 'numeric', month: '2-digit', day: '2-digit' });
const diaLima = t => fmtClave.format(new Date(t));
function dia(t) {
  const hoy = diaLima(Date.now()), man = diaLima(Date.now() + 864e5), d = diaLima(t);
  if (d === hoy) return 'hoy';
  if (d === man) return 'mañana';
  return fmtDia.format(new Date(t)).replace(/[.,]/g, '');
}
const fechaHora = t => `${dia(t)}, ${fmtHora.format(new Date(t))}`;
const hora = t => fmtHora.format(new Date(t));
function distancia(m) { return m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1).replace('.', ',')} km`; }
function metros(a, b) { // [lat, lon]
  const mLon = 111320 * Math.cos(a[0] * Math.PI / 180), mLat = 110540;
  return Math.hypot((a[1] - b[1]) * mLon, (a[0] - b[0]) * mLat);
}
function mesAnio(p) {
  const s = String(p ?? '').trim();
  if (!/^\d{6}$/.test(s)) return null;
  const m = +s.slice(4); if (m < 1 || m > 12) return null;
  return new Intl.DateTimeFormat('es-PE', { month: 'long', timeZone: 'UTC' }).format(new Date(Date.UTC(+s.slice(0, 4), m - 1, 15))) + ' de ' + s.slice(0, 4);
}
function aviso(texto) { const e = $('estadoMapa'); e.hidden = !texto; e.textContent = texto || ''; }

/* ---------- consultas ArcGIS (solo lectura) ---------- */
async function consultarTodo(capa, params, pagina = 1000) {
  const salida = [];
  for (let offset = 0; offset < 50000;) {
    const q = new URLSearchParams({ where: '1=1', outSR: '4326', ...params, resultOffset: String(offset),
                                    resultRecordCount: String(pagina), f: 'geojson' });
    const r = await fetch(`${GEOSUNASS}${capa}/FeatureServer/0/query?${q}`);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (d.error) throw new Error('ArcGIS');
    const f = d.features || [];
    salida.push(...f);
    if (!(d.properties?.exceededTransferLimit || d.exceededTransferLimit) || !f.length) break;
    offset += f.length;
  }
  return salida;
}
function poligonos(geom) {
  if (!geom) return [];
  if (geom.type === 'Polygon') return [geom.coordinates];
  if (geom.type === 'MultiPolygon') return geom.coordinates;
  return [];
}
function dentro(anillo, lat, lon) { // even-odd, anillo de [lon, lat]
  let c = false;
  for (let i = 0, j = anillo.length - 1; i < anillo.length; j = i++) {
    const [xi, yi] = anillo[i], [xj, yj] = anillo[j];
    if ((yi > lat) !== (yj > lat) && lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) c = !c;
  }
  return c;
}
function contiene(polis, lat, lon) {
  return polis.some(p => p.length && dentro(p[0], lat, lon) && !p.slice(1).some(h => dentro(h, lat, lon)));
}
function distanciaA(polis, lat, lon) {
  if (contiene(polis, lat, lon)) return 0;
  const mLon = 111320 * Math.cos(lat * Math.PI / 180), mLat = 110540;
  let min = Infinity;
  for (const p of polis) for (const anillo of p) for (let i = 1; i < anillo.length; i++) {
    const ax = (anillo[i - 1][0] - lon) * mLon, ay = (anillo[i - 1][1] - lat) * mLat;
    const bx = (anillo[i][0] - lon) * mLon, by = (anillo[i][1] - lat) * mLat;
    const dx = bx - ax, dy = by - ay, l = dx * dx + dy * dy;
    const t = l ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / l)) : 0;
    min = Math.min(min, Math.hypot(ax + t * dx, ay + t * dy));
  }
  return min;
}

/* ---------- mapa ---------- */
const mapa = L.map('mapa', { zoomControl: true }).setView([-12.05, -77.03], 12);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(mapa);
for (const [n, z] of [['inundables', 350], ['lluvias', 405], ['sectores', 410], ['cortes', 420], ['puntos', 610], ['marcasCortes', 630]]) {
  mapa.createPane(n).style.zIndex = z;
}
// One canvas per pane: a shared canvas draws every layer in the same pane, so later layers covered the cortes.
const lienzos = Object.fromEntries(['sectores', 'cortes', 'lluvias'].map(p => [p, L.canvas({ padding: 0.5, pane: p })]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const estado = {
  referencia: null,          // {lat, lon, nombre, tipo:'ubicacion'|'direccion'}
  pestana: 'cortes',
  detalle: null,             // función que dibuja una ficha en el panel
  cortes: null, cortesFecha: null, cortesError: false, zonaCortes: 'lima',
  puntos: null, puntosError: false, soloOperativos: true, grupo: 'todos',
  sectores: null, sectoresCargando: false,
  aniegos: null, sombra: null,
};

/* ---------- capas activables ---------- */
const CAPAS = [
  { id: 'cortes', nombre: 'Cortes de agua', nota: 'Reportes de las empresas a Sunass, sin verificar. El horario puede cambiar.',
    defecto: true, leyenda: () => [['En curso', MAPA.naranja, 1], ['Próximo', MAPA.azul, 1]] },
  { id: 'sectores', nombre: 'Horas de agua al día', nota: 'Promedio por sector que reporta cada empresa a Sunass.',
    defecto: false, leyenda: () => NIVELES.map(n => [n.corto, n.color, 0.7]) },
  { id: 'puntos', nombre: 'Puntos de abastecimiento', nota: 'Hidrantes y cámaras especiales para emergencias (Lima y Callao).',
    defecto: true, leyenda: () => [['Hidrante', '#fabd0d', 1], ['Cámara especial', '#0d66d1', 1], ['No operativo', '#8e8e93', 1]] },
  { id: 'aniegos', nombre: 'Aniegos recientes', nota: 'Medios y fuentes oficiales, últimas 48 h. Puntos referenciales.',
    defecto: true, leyenda: () => [['Incidente revisado', '#0d66d1', 1]] },
  { id: 'inundables', nombre: 'Zonas inundables', nota: 'MINAM · Modelo T100. Capa preventiva: no muestra aniegos actuales ni el riesgo de cada vivienda. Sin color no garantiza seguridad.',
    defecto: false, leyenda: () => [['Modelo de inundación', '#d11b6b', 0.8]] },
  { id: 'lluvias', nombre: 'Riesgo por lluvias', nota: 'Escenarios por distrito de CENEPRED según el aviso de lluvias. No son aniegos actuales; un nivel bajo no significa ausencia de peligro.',
    defecto: false, leyenda: () => RIESGOS.map(r => [r.nivel, r.color, 0.6]) },
  { id: 'ana', nombre: 'Puntos críticos ANA', nota: 'Inventario histórico con el año de cada punto. No son emergencias actuales.',
    defecto: false, leyenda: () => [['Punto crítico', '#ff9500', 1]] },
];
const racimo = clase => L.markerClusterGroup({ clusterPane: 'puntos', showCoverageOnHover: false, maxClusterRadius: 50,
  iconCreateFunction: g => L.divIcon({ className: '', iconSize: [34, 34], html: `<div class="racimo ${clase}">${g.getChildCount()}</div>` }) });
const grupos = Object.fromEntries(CAPAS.map(c => [c.id, c.id === 'puntos' ? racimo('punto') : c.id === 'ana' ? racimo('ana') : L.layerGroup()]));
function activa(id) { const v = leer('capa_' + id); return v ? v.valor : CAPAS.find(c => c.id === id).defecto; }
function pintarCapas() {
  const caja = $('capas');
  caja.replaceChildren(...CAPAS.map(c => {
    const casilla = el('input', { type: 'checkbox', id: 'capa-' + c.id, class: 'interruptor', role: 'switch' });
    casilla.checked = activa(c.id);
    casilla.addEventListener('change', () => { guardar('capa_' + c.id, casilla.checked); aplicarCapa(c.id); });
    const ley = el('div', { class: 'leyenda' }, ...c.leyenda().map(([t, color, op, discontinua]) => {
      const m = el('span', { class: 'muestra' });
      m.style.background = color; m.style.opacity = op;
      if (discontinua) { m.style.background = 'transparent'; m.style.border = `1.5px dashed ${color}`; m.style.opacity = 1; }
      return el('span', {}, m, t);
    }));
    return el('div', { class: 'capa' }, el('label', { for: 'capa-' + c.id }, casilla, el('span', { class: 'nombre-capa', text: c.nombre })),
      el('p', { class: 'nota', text: c.nota }), ley);
  }));
}
function pintarLeyenda() {
  // Compact legend on the map for the active layers that need one.
  const cajas = CAPAS.filter(c => activa(c.id) && ['cortes', 'sectores', 'lluvias', 'inundables'].includes(c.id)).map(c =>
    el('div', { class: 'caja' }, el('strong', { text: c.id === 'sectores' ? 'Horas de agua' : c.nombre }),
      ...c.leyenda().map(([t, color, op]) => { const m = el('span', { class: 'muestra' }); m.style.background = color; m.style.opacity = op; return el('span', {}, m, t); })));
  $('leyendaMapa').replaceChildren(...cajas);
}
$('botonCapas').addEventListener('click', () => {
  const abierto = $('popoverCapas').hidden;
  $('popoverCapas').hidden = !abierto; $('botonCapas').setAttribute('aria-expanded', String(abierto));
});
$('cerrarCapas').addEventListener('click', () => { $('popoverCapas').hidden = true; $('botonCapas').setAttribute('aria-expanded', 'false'); });
function aplicarCapa(id) {
  const on = activa(id);
  if (on) grupos[id].addTo(mapa); else mapa.removeLayer(grupos[id]);
  if (on) ({ cortes: cargarCortes, sectores: cargarSectores, puntos: cargarPuntos, aniegos: cargarAniegos,
             inundables: activarInundables, ana: cargarAna, lluvias: cargarLluvias })[id]?.();
  pintarLeyenda(); pintarPestanas();
}

/* ---------- 1. cortes de agua ---------- */
function filtroCortes() {
  const ahora = new Date().toISOString().slice(0, 19).replace('T', ' ');
  return `situacion='SERVICIO INTERRUMPIDO' AND c_digo NOT LIKE '%-FAN%' AND fecha_estimada_de_restablecimie >= TIMESTAMP '${ahora}'`;
}
function agruparCortes(features) {
  const grupos = new Map();
  for (const f of features) {
    const p = f.properties || {};
    const inicio = p.fecha_de_inicio, fin = p.fecha_estimada_de_restablecimie, codigo = limpio(p.c_digo, 80);
    if (inicio == null || fin == null || fin <= inicio || !codigo) continue;
    const k = `${codigo}|${inicio}|${fin}`;
    if (!grupos.has(k)) grupos.set(k, []);
    grupos.get(k).push(f);
  }
  const cortes = [];
  for (const filas of grupos.values()) {
    filas.sort((a, b) => a.properties.objectid - b.properties.objectid);
    const p = filas[0].properties;
    const unicos = campo => [...new Set(filas.map(f => limpio(f.properties[campo], 80)).filter(Boolean))];
    const tipo = limpio(p.tipo_de_interrupci_n).toUpperCase();
    const conex = filas.map(f => f.properties.n_de_conexiones_afectadas).filter(n => n > 0).map(n => Math.round(n));
    cortes.push({
      id: p.objectid, codigo: limpio(p.c_digo, 80), tipo: tipo === 'PROGRAMADA' ? 'programado' : tipo === 'IMPREVISTA' ? 'imprevisto' : 'otro',
      eps: limpio(p.eps, 80), localidad: limpio(p.localidad, 200), motivo: limpio(p.motivo_de_interrupci_n, 120),
      detalle: limpio(p.detalle_de_motivo, 600), inicio: p.fecha_de_inicio, fin: p.fecha_estimada_de_restablecimie,
      departamentos: unicos('departamento'), distritos: unicos('distrito'), sectores: unicos('sector'),
      conexiones: !conex.length ? null : conex.every(n => n === conex[0]) ? conex[0] : conex.reduce((a, b) => a + b, 0),
      poligonos: filas.flatMap(f => poligonos(f.geometry)),
    });
  }
  return cortes.sort((a, b) => a.id - b.id);
}
const estadoCorte = c => Date.now() >= c.fin ? 'vencido' : Date.now() >= c.inicio ? 'enCurso' : 'proximo';
const zona = c => c.distritos.map(titulo).join(', ') || 'Zona del reporte';
const tipoCorte = c => ({ programado: 'Corte programado', imprevisto: 'Corte imprevisto' })[c.tipo] || 'Corte de agua';
const sectoresLegibles = c => c.sectores.map(s => /^\d+$/.test(s) ? 'Sector ' + s : titulo(s)).join(', ');
async function cargarCortes(forzar) {
  if (estado.cortes && !forzar && Date.now() - estado.cortesFecha < 15 * 60000) return dibujarCortes();
  const cache = leer('cortes_v1');
  if (cache && !estado.cortes) { estado.cortes = cache.valor; estado.cortesFecha = cache.fecha; dibujarCortes(); }
  try {
    const f = await consultarTodo(CAPA_CORTES, {
      where: filtroCortes(), orderByFields: 'objectid', maxAllowableOffset: '0.0001', geometryPrecision: '5',
      outFields: 'objectid,c_digo,tipo_de_interrupci_n,eps,departamento,distrito,localidad,sector,motivo_de_interrupci_n,detalle_de_motivo,fecha_de_inicio,fecha_estimada_de_restablecimie,n_de_conexiones_afectadas',
    }, 250);
    estado.cortes = agruparCortes(f); estado.cortesFecha = Date.now(); estado.cortesError = false;
    guardar('cortes_v1', estado.cortes);
  } catch { estado.cortesError = true; }
  dibujarCortes();
}
const MAPA = { naranja: '#e8590c', azul: '#1c62e6' };
// Crossed-out drop: a water drop with a slash (no water). The slash gap uses the marker colour.
const gotaTachada = (fondo, tam = 18) => `<svg viewBox="0 0 24 24" width="${tam}" height="${tam}" aria-hidden="true">
  <path fill="#fff" d="M12 2.5c-3.3 4.4-6.5 8.2-6.5 11.6A6.5 6.5 0 0 0 12 20.6a6.5 6.5 0 0 0 6.5-6.5C18.5 10.7 15.3 6.9 12 2.5z"/>
  <line x1="3.5" y1="3.5" x2="20.5" y2="20.5" stroke="${fondo}" stroke-width="5" stroke-linecap="round"/>
  <line x1="3.5" y1="3.5" x2="20.5" y2="20.5" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>`;
const capasCorte = new Map(), marcasCorte = new Map();
let corteElegido = null;
function estiloCorte(c, elegido) {
  const enCurso = estadoCorte(c) === 'enCurso', color = enCurso ? MAPA.naranja : MAPA.azul;
  return { color, weight: elegido ? 4.5 : 3, opacity: 1, fillColor: color, fillOpacity: (enCurso ? 0.35 : 0.28) + (elegido ? 0.17 : 0),
           dashArray: enCurso ? null : '8 5', lineJoin: 'round' };
}
function dibujarCortes() {
  grupos.cortes.clearLayers(); capasCorte.clear(); marcasCorte.clear();
  // At city or country scale the areas are a few pixels (the iOS map has the same problem): a marker sits
  // at the centre of each cut. Overlapping markers are grouped; a small group opens a list instead of zooming.
  const marcas = L.markerClusterGroup({ clusterPane: 'marcasCortes', showCoverageOnHover: false, maxClusterRadius: 40,
    zoomToBoundsOnClick: false, spiderfyOnMaxZoom: false,
    iconCreateFunction: g => { const curso = g.getAllChildMarkers().some(m => m.options.enCurso);
      return L.divIcon({ className: '', iconSize: [48, 32], iconAnchor: [24, 16],
        html: `<div class="racimo corte${curso ? ' en-curso' : ''}">${gotaTachada(curso ? MAPA.naranja : MAPA.azul, 16)}<span>${g.getChildCount()}</span></div>` }); } });
  marcas.on('clusterclick', e => {
    const cortes = e.layer.getAllChildMarkers().map(m => m.options.corte);
    if (cortes.length <= 5 || mapa.getZoom() >= 16) seleccionarGrupo(cortes);
    else e.layer.zoomToBounds({ padding: [50, 50] });
  });
  for (const c of estado.cortes || []) {
    const s = estadoCorte(c);
    if (s === 'vencido' || !c.poligonos.length) continue;
    const enCurso = s === 'enCurso';
    const geo = { type: 'MultiPolygon', coordinates: c.poligonos };
    // The base map is always light, even in dark mode: fixed saturated colours plus a white halo.
    L.geoJSON(geo, { pane: 'cortes', renderer: lienzos.cortes, smoothFactor: 0, interactive: false,
      style: { color: '#ffffff', weight: 6, opacity: 0.95, fill: false, lineJoin: 'round' } }).addTo(grupos.cortes);
    const capa = L.geoJSON(geo, { pane: 'cortes', renderer: lienzos.cortes, smoothFactor: 0, style: estiloCorte(c, c.id === corteElegido) });
    capa.on('click', e => { L.DomEvent.stop(e); seleccionarCorte(c); });
    capa.addTo(grupos.cortes);
    capasCorte.set(c.id, capa);
    const marca = L.marker(capa.getBounds().getCenter(), { pane: 'marcasCortes', enCurso, corte: c, keyboard: true,
      title: `${enCurso ? 'Corte en curso' : 'Próximo corte'} · ${zona(c)}`,
      icon: L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 17],
        html: `<div class="marca-corte ${enCurso ? 'en-curso' : 'proximo'}${c.id === corteElegido ? ' elegido' : ''}">${gotaTachada(enCurso ? MAPA.naranja : MAPA.azul)}</div>` }) })
      .on('click', () => seleccionarCorte(c));
    marca.addTo(marcas);
    marcasCorte.set(c.id, marca);
  }
  marcas.addTo(grupos.cortes);
  if (estado.pestana === 'cortes') pintarContenido();
}
function resaltarCorte(id) {
  for (const [cid, capa] of capasCorte) {
    const c = (estado.cortes || []).find(x => x.id === cid);
    if (c) capa.setStyle(estiloCorte(c, cid === id));
  }
  for (const [cid, m] of marcasCorte) m.getElement()?.firstElementChild?.classList.toggle('elegido', cid === id);
  corteElegido = id;
}
function relacionCorte(c) {
  const r = estado.referencia;
  if (!r) return null;
  const d = distanciaA(c.poligonos, r.lat, r.lon);
  return { d, texto: d === 0 ? (r.tipo === 'ubicacion' ? 'Afecta tu ubicación' : 'Afecta esta dirección') : `A ${distancia(d)} · No afecta este lugar` };
}
function horarioCorte(c) {
  const s = estadoCorte(c), mismoDia = diaLima(c.inicio) === diaLima(c.fin);
  return s === 'enCurso' ? `Vuelve aprox. ${fechaHora(c.fin)}` :
    s === 'proximo' ? (mismoDia ? `${capital(dia(c.inicio))}, ${hora(c.inicio)}–${hora(c.fin)}` : `${fechaHora(c.inicio)} a ${fechaHora(c.fin)}`) :
    `Debía terminar ${fechaHora(c.fin)}`;
}
const chipEstado = c => { const s = estadoCorte(c);
  return el('span', { class: 'chip ' + (s === 'enCurso' ? 'naranja' : s === 'proximo' ? 'acento' : 'gris'),
                      text: s === 'enCurso' ? 'En curso' : s === 'proximo' ? 'Próximo' : 'Ya debía terminar' }); };
const chipTipo = c => el('span', { class: 'chip', text: ({ programado: 'Programado', imprevisto: 'Imprevisto' })[c.tipo] || 'Reporte de agua' });
function resumenCorte(c) {
  const rel = relacionCorte(c);
  return [el('div', { class: 'acciones', style: 'margin-top:0' }, chipEstado(c), chipTipo(c)),
    el('h3', { text: zona(c) }), el('p', { text: horarioCorte(c) }),
    c.motivo ? el('p', { class: 'suave', text: capital(c.motivo) }) : null,
    rel ? el('p', { class: 'pequeno', text: rel.texto }) : null];
}
function seleccionarCorte(c, mover) {
  resaltarCorte(c.id);
  seleccionar(resumenCorte(c), () => fichaCorte(c));
  if (mover) encuadrar(c.poligonos);
}
function seleccionarGrupo(cortes) {
  resaltarCorte(null);
  const filas = cortes.sort((a, b) => (estadoCorte(a) === 'enCurso' ? 0 : 1) - (estadoCorte(b) === 'enCurso' ? 0 : 1) || a.inicio - b.inicio)
    .map(c => el('button', { type: 'button', class: 'fila-grupo', onclick: () => seleccionarCorte(c) },
      el('div', { class: 'acciones', style: 'margin-top:0' }, chipEstado(c), chipTipo(c)),
      el('strong', { text: zona(c) }), el('div', { class: 'pequeno suave', text: horarioCorte(c) })));
  seleccionar([el('h3', { text: `${cortes.length} reportes en esta zona` }),
    el('p', { class: 'pequeno suave', text: 'Elige uno para ver su detalle.' }), ...filas], null);
}
function tarjetaCorte(c) {
  const rel = relacionCorte(c);
  return el('article', { class: 'item' + (rel && rel.d === 0 ? ' destacado' : ''), tabindex: '0',
      onclick: () => seleccionarCorte(c, true), onkeydown: e => { if (e.key === 'Enter') seleccionarCorte(c, true); } },
    el('div', { class: 'acciones', style: 'margin-top:0' }, chipEstado(c), chipTipo(c)),
    el('h3', { text: zona(c) }), el('p', { text: horarioCorte(c) }),
    c.motivo ? el('p', { class: 'suave', text: capital(c.motivo) }) : null,
    rel ? el('p', { class: 'pequeno', text: rel.texto }) : null);
}
function encuadrar(polis) {
  const pts = polis.flatMap(p => p[0] || []).map(([x, y]) => [y, x]);
  // Leave room at the bottom for the selection card so it never covers the zone.
  if (pts.length) mapa.fitBounds(pts, { paddingTopLeft: [50, 70], paddingBottomRight: [50, 230], maxZoom: 16 });
}
function listaCortes() {
  const nodos = [];
  if (!estado.cortes) return [el('p', { class: 'vacio', text: estado.cortesError ? 'Sin datos disponibles. No se pudieron descargar los reportes. Revisa tu conexión.' : 'Cargando cortes…' })];
  if (estado.cortesError) nodos.push(el('p', { class: 'bloque' }, el('span', { class: 'aviso pequeno', text: `Sin conexión · datos de ${fechaHora(estado.cortesFecha)}` })));
  let vigentes = estado.cortes.filter(c => estadoCorte(c) !== 'vencido');
  const r = estado.referencia;
  if (r) vigentes = vigentes.filter(c => distanciaA(c.poligonos, r.lat, r.lon) <= 3000);
  else if (estado.zonaCortes === 'lima') vigentes = vigentes.filter(c => c.departamentos.some(d => /^(LIMA|CALLAO)$/i.test(d)));
  const ahora = vigentes.filter(c => estadoCorte(c) === 'enCurso')
    .sort((a, b) => r ? (distanciaA(a.poligonos, r.lat, r.lon) - distanciaA(b.poligonos, r.lat, r.lon)) || a.fin - b.fin : a.fin - b.fin);
  const proximos = vigentes.filter(c => estadoCorte(c) === 'proximo').sort((a, b) => a.inicio - b.inicio);
  if (r) nodos.push(el('p', { class: 'bloque pequeno suave', text: `Cortes a menos de 3 km de ${r.nombre}` }));
  else {
    const sel = el('select', { 'aria-label': 'Zona de consulta', onchange: e => { estado.zonaCortes = e.target.value; pintarPestanas(); pintarContenido(); } },
      ...[['lima', 'Lima y Callao'], ['peru', 'Todo el Perú']].map(([v, t]) => { const o = el('option', { value: v, text: t }); o.selected = estado.zonaCortes === v; return o; }));
    nodos.push(el('div', { class: 'fila-filtros' }, sel, el('span', { class: 'pequeno suave', text: 'o busca una dirección' })));
  }
  nodos.push(el('div', { class: 'resumen' },
    el('div', { class: 'dato' }, el('strong', { text: String(ahora.length), style: 'color:var(--corte-curso)' }), el('span', { text: 'en curso' })),
    el('div', { class: 'dato' }, el('strong', { text: String(proximos.length), style: 'color:var(--acento)' }), el('span', { text: 'próximos' }))));
  nodos.push(el('h2', { text: 'En curso' }));
  if (!ahora.length) nodos.push(el('p', { class: 'vacio', text: 'Sin cortes reportados en esta zona. Algunos cortes se reportan tarde. Esto no garantiza que tengas agua.' }));
  nodos.push(...ahora.map(tarjetaCorte));
  nodos.push(el('h2', { text: 'Próximos' }));
  if (!proximos.length) nodos.push(el('p', { class: 'vacio', text: 'Sin próximos cortes reportados.' }));
  let diaActual = null;
  for (const c of proximos) {
    const d = diaLima(c.inicio);
    if (d !== diaActual) { diaActual = d; nodos.push(el('p', { class: 'bloque pequeno', style: 'font-weight:600', text: capital(dia(c.inicio)) })); }
    nodos.push(tarjetaCorte(c));
  }
  nodos.push(el('p', { class: 'bloque pequeno suave', text: `El horario puede cambiar. Actualizado ${fechaHora(estado.cortesFecha)}. Fuente: reportes de las empresas a Sunass, sin verificar.` }),
    el('div', { class: 'bloque' }, el('button', { class: 'boton pequeno', type: 'button', text: 'Actualizar reportes', onclick: () => cargarCortes(true) })));
  return nodos;
}
function fichaCorte(c) {
  const datos = [['Motivo', capital(c.motivo)], ['Empresa de agua', c.eps], ['Inicio del corte', fechaHora(c.inicio)],
    ['Sectores', sectoresLegibles(c)]];
  const mas = [['Detalle', c.detalle], ['Localidad', titulo(c.localidad)], ['Tipo', tipoCorte(c)],
    ['Conexiones afectadas', c.conexiones ? `Aprox. ${c.conexiones.toLocaleString('es-PE')}` : ''], ['Código del reporte', c.codigo]];
  const ficha = pares => el('dl', { class: 'ficha' }, ...pares.filter(([, v]) => v).flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  const rel = relacionCorte(c);
  return [
    el('div', { class: 'bloque' },
      el('div', { class: 'acciones', style: 'margin-top:0' }, chipEstado(c), el('span', { class: 'chip', text: tipoCorte(c) })),
      el('h2', { class: 'titulo-ficha', text: zona(c) }),
      el('p', { class: 'suave pequeno', text: estadoCorte(c) === 'proximo' ? 'Horario estimado' : 'Reposición estimada', style: 'margin-top:10px' }),
      el('p', { class: 'grande', text: estadoCorte(c) === 'proximo' ? horarioCorte(c) : fechaHora(c.fin) }),
      el('p', { class: 'pequeno suave', text: 'El horario puede cambiar.' }),
      rel ? el('p', { style: 'font-weight:600', text: rel.texto }) : null),
    ficha(datos),
    el('details', { class: 'bloque' }, el('summary', { text: 'Más información del reporte' }), ficha(mas)),
    el('div', { class: 'bloque acciones' }, el('button', { class: 'boton', type: 'button', text: 'Ver zona en el mapa', onclick: () => { resaltarCorte(c.id); encuadrar(c.poligonos); } })),
    el('p', { class: 'bloque pequeno suave', text: `Reporte a Sunass · descargado ${fechaHora(estado.cortesFecha)}. Zona y horario estimados. Sunass no ha verificado este reporte; confirma con tu empresa de agua.` }),
  ];
}

/* ---------- 2. sectores y continuidad ---------- */
const NIVELES = [
  { id: 'critica', titulo: 'Menos de 6 h', corto: '<6h', color: '#ff3b30', texto: 'El agua llega solo un rato al día. Las familias dependen de tanques, cilindros o cisternas.' },
  { id: 'baja', titulo: '6 a 12 h', corto: '6–12h', color: '#ff9500', texto: 'Hay agua parte del día, por ejemplo solo en la mañana.' },
  { id: 'media', titulo: '12 a 20 h', corto: '12–20h', color: '#ffcc00', texto: 'Hay agua la mayor parte del día, con algunos cortes.' },
  { id: 'alta', titulo: '20 h o más', corto: '20h+', color: '#34c759', texto: 'Servicio prácticamente continuo, todo el día.' },
  { id: 'sinDato', titulo: 'Sin dato', corto: 'Sin dato', color: '#8e8e93', texto: 'La empresa de agua no reportó este dato.' },
];
function horasSector(p) {
  const h = Number(p.continuida);
  if (p.continuida == null || !isFinite(h)) return null;
  if (h === 0 && !String(p.cont_fecha ?? '').trim()) return null; // 0 sin periodo = sin dato, no "sin servicio"
  return h;
}
const nivel = h => NIVELES[h == null ? 4 : h < 6 ? 0 : h < 12 ? 1 : h < 20 ? 2 : 3];
let sectorElegido = null;
async function cargarSectores() {
  if (estado.sectores || estado.sectoresCargando) return dibujarSectores();
  const cache = leer('sectores_v1');
  if (cache) { estado.sectores = cache.valor; dibujarSectores(); return; }
  estado.sectoresCargando = true; aviso('Cargando sectores…');
  try {
    const f = await consultarTodo('sectores_continuidad_join', {
      orderByFields: 'objectid', maxAllowableOffset: '0.0001', geometryPrecision: '5',
      outFields: 'objectid,sector,eps,localidad,distrito,departamen,continuida,cont_fecha,presion,presion_fecha,criticidad',
    }, 250);
    estado.sectores = { type: 'FeatureCollection', features: f.filter(x => poligonos(x.geometry).length) };
    guardar('sectores_v1', estado.sectores);
    aviso(null);
  } catch { aviso('No se pudieron cargar los sectores'); }
  estado.sectoresCargando = false;
  dibujarSectores();
}
function estiloSector(f) {
  const n = nivel(horasSector(f.properties)), sel = f === sectorElegido;
  return { color: sel ? css('--texto') : n.color, opacity: sel ? 1 : 0.8, weight: sel ? 2.5 : 0.75,
           fillColor: n.color, fillOpacity: sel ? 0.55 : 0.28 };
}
let capaSectores = null;
function dibujarSectores() {
  grupos.sectores.clearLayers();
  if (!estado.sectores) return;
  capaSectores = L.geoJSON(estado.sectores, { pane: 'sectores', renderer: lienzos.sectores, smoothFactor: 0.5, style: estiloSector,
    onEachFeature: (f, capa) => capa.on('click', e => { L.DomEvent.stop(e); sectorElegido = f; capaSectores.setStyle(estiloSector);
      const h = horasSector(f.properties), n = nivel(h);
      seleccionar([el('span', { class: 'chip', style: `background:${n.color};color:#111;border-color:transparent`, text: h == null ? 'Sin dato' : n.titulo }),
        el('h3', { text: nombreSector(f.properties) }),
        el('p', { text: h == null ? 'Sin dato de continuidad' : `${String(Math.round(h * 10) / 10).replace('.', ',')} horas de agua al día` }),
        el('p', { class: 'pequeno suave', text: limpio(f.properties.eps, 80) })], () => fichaSector(f.properties)); }) });
  capaSectores.addTo(grupos.sectores);
}
function nombreSector(p) {
  const n = limpio(p.sector, 120) || 'Sector sin nombre';
  return /sector/i.test(n) ? titulo(n) : 'Sector ' + titulo(n);
}
function fichaSector(p) {
  const h = horasSector(p), n = nivel(h);
  const ubic = [...new Set([p.localidad, p.distrito, p.departamen].map(x => limpio(x, 80)).filter(Boolean).map(titulo))].join(', ');
  const med = el('div', { class: 'medidor' }, el('span')); med.firstChild.style.width = h == null ? '0' : Math.min(100, h / 24 * 100) + '%';
  med.firstChild.style.background = n.color;
  const presion = Number(p.presion) > 0 ? `${Number(p.presion).toFixed(1).replace('.', ',')} m.c.a.` : null;
  return [el('div', { class: 'bloque' },
    el('h2', { text: nombreSector(p), style: 'margin:6px 0 2px' }), el('p', { class: 'suave', text: ubic }),
    p.eps ? el('p', { text: 'Empresa de agua: ' + limpio(p.eps, 80) }) : null,
    h == null ? el('p', { class: 'grande', text: 'Sin dato de continuidad' }) :
      el('div', {}, el('p', { class: 'grande', text: `${(Math.round(h * 10) / 10).toString().replace('.', ',')} horas de agua al día` }), med,
        el('div', { class: 'pequeno suave', text: '0h · 24h' }), mesAnio(p.cont_fecha) ? el('p', { class: 'pequeno', text: 'Medición de ' + mesAnio(p.cont_fecha) }) : null),
    presion ? el('p', { text: `Presión: ${presion}` + (mesAnio(p.presion_fecha) ? ` · Medición de presión: ${mesAnio(p.presion_fecha)}` : '') }) : null,
    p.criticidad == 1 ? el('p', { style: 'color:#ff9500', text: '⚠ Sunass marca este sector como crítico' }) : null,
    el('p', { class: 'pequeno suave', text: 'Fuente: Sunass (GeoSunass). Las horas de servicio son un promedio que reporta la empresa de agua y pueden variar día a día.' }),
    el('details', {}, el('summary', { text: '¿Qué muestra este mapa?' }),
      el('p', { class: 'pequeno', text: 'La ciudad está dividida en sectores: zonas que reciben el agua por la misma red de tuberías. Cada sector se pinta según cuántas horas al día tiene agua por la tubería, en promedio.' }),
      ...NIVELES.map(x => el('p', { class: 'pequeno' }, el('strong', { text: x.titulo + ': ' }), x.texto)),
      el('p', { class: 'pequeno', text: 'Son promedios que cada empresa de agua (SEDAPAL en Lima) reporta a Sunass. Pueden cambiar de un día a otro y no reflejan cortes por emergencias o mantenimiento.' })))];
}

/* ---------- 3. puntos de abastecimiento ---------- */
const iconos = {
  hidrante: on => L.icon({ iconUrl: 'img/pin_hidrante.png', iconSize: [37, 48], iconAnchor: [18, 48], popupAnchor: [0, -44], className: 'punto-mapa' + (on ? '' : ' inactivo') }),
  camara: on => L.icon({ iconUrl: 'img/pin_camara.png', iconSize: [37, 48], iconAnchor: [18, 48], popupAnchor: [0, -44], className: 'punto-mapa' + (on ? '' : ' inactivo') }),
};
function direccionLimpia(t) {
  // Some rows use "calle: X, localidad: Y, MZ: A, LOTE: 11, N° : 0": keep only the filled parts, in the same order.
  if (!/^\s*calle\s*:/i.test(t)) return t;
  const partes = {}, re = /(calle|localidad|mz|lote|n°)\s*:\s*([^:]*?)(?=,\s*(?:calle|localidad|mz|lote|n°)\s*:|$)/gi;
  for (const m of t.matchAll(re)) partes[m[1].toLowerCase()] = m[2].trim().replace(/,$/, '');
  const util = v => v && v !== '0';
  const via = [partes.calle, util(partes['n°']) ? partes['n°'] : null].filter(Boolean).join(' ');
  const lote = [util(partes.mz) ? 'Mz ' + partes.mz : null, util(partes.lote) ? 'Lt ' + partes.lote : null].filter(Boolean).join(' ');
  return [via, lote, partes.localidad].filter(Boolean).join(', ') || t;
}
function punto(f) {
  const p = f.properties || {}, c = f.geometry?.coordinates;
  if (!Array.isArray(c) || c.length < 2) return null;
  const t = k => limpio(p[k], 200);
  const ubic = direccionLimpia(t('ubicación')), dist = t('distrito');
  const direccion = !ubic ? titulo(dist) : (dist && plano(ubic).includes(plano(dist)) ? ubic : `${ubic} · ${titulo(dist)}`);
  return { id: p.fid, nombre: t('nombre_o_c') || 'Sin nombre', tipo: t('tipo_de_co') || 'Otro',
           grupo: /c[aá]mara/i.test(t('tipo_com2') || 'Hidrante') ? 'camara' : 'hidrante', centro: t('eomr'),
           direccion, capacidad: Number(p.capacidad) || 0, situacion: t('situación') || 'Desconocida',
           distrito: dist, lat: c[1], lon: c[0], operativo: (t('situación') || 'Desconocida') === 'Operativo' };
}
async function cargarPuntos() {
  if (estado.puntos) return dibujarPuntos();
  const cache = leer('puntos_agua_web_v2');
  if (cache) { estado.puntos = cache.valor.map(x => ({ ...x, operativo: x.situacion === 'Operativo' })); dibujarPuntos(); }
  try {
    const f = await consultarTodo('Puntos_con_abastecimiento_FINAL', {
      orderByFields: 'fid', outFields: 'fid,nombre_o_c,tipo_de_co,tipo_com2,eomr,ubicación,capacidad,situación,estado,distrito' });
    estado.puntos = f.map(punto).filter(Boolean);
    guardar('puntos_agua_web_v2', estado.puntos);
    estado.puntosError = false;
  } catch { estado.puntosError = !estado.puntos; }
  dibujarPuntos();
}
const puntosFiltrados = () => (estado.puntos || []).filter(p => (!estado.soloOperativos || p.operativo) && (estado.grupo === 'todos' || p.grupo === estado.grupo));
function dibujarPuntos() {
  grupos.puntos.clearLayers();
  for (const p of puntosFiltrados()) {
    L.marker([p.lat, p.lon], { icon: iconos[p.grupo](p.operativo), pane: 'puntos', title: p.nombre })
      .on('click', () => seleccionarPunto(p)).addTo(grupos.puntos);
  }
  if (estado.pestana === 'puntos') pintarContenido();
}
const grupoNombre = p => p.grupo === 'camara' ? 'Cámara especial' : 'Hidrante';
function seleccionarPunto(p, mover) {
  const r = estado.referencia, d = r ? metros([r.lat, r.lon], [p.lat, p.lon]) : null;
  seleccionar([el('span', { class: 'chip' + (p.operativo ? '' : ' gris'), text: p.operativo ? grupoNombre(p) : `${grupoNombre(p)} · ${p.situacion}` }),
    el('h3', { text: p.nombre }), el('p', { class: 'suave', text: p.direccion }),
    d != null ? el('p', { class: 'pequeno', text: `${distancia(d)} · aprox. ${Math.max(1, Math.round(d * 1.3 / 75))} min a pie` }) : null,
    el('div', { class: 'acciones' }, enlaceSeguro(`https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}&travelmode=walking`, 'Cómo llegar ↗'))],
    () => fichaPunto(p));
  if (mover) { mapa.setView([p.lat, p.lon], 17); mapa.panBy([0, 110]); }
}
function tarjetaPunto(p) {
  const r = estado.referencia, d = r ? metros([r.lat, r.lon], [p.lat, p.lon]) : null;
  return el('article', { class: 'item', tabindex: '0', onclick: () => seleccionarPunto(p, true),
      onkeydown: e => { if (e.key === 'Enter') seleccionarPunto(p, true); } },
    el('span', { class: 'chip' + (p.operativo ? '' : ' gris'), text: p.operativo ? grupoNombre(p) : `${grupoNombre(p)} · ${p.situacion}` }),
    el('h3', { text: p.nombre }), el('p', { class: 'suave', text: p.direccion }),
    d != null ? el('p', { class: 'pequeno', text: `${distancia(d)} · aprox. ${Math.max(1, Math.round(d * 1.3 / 75))} min a pie` }) : null);
}
function listaPuntos() {
  if (!estado.puntos) return [el('p', { class: 'bloque suave', text: estado.puntosError ? 'Sin datos. No se pudo descargar la información. Revisa tu conexión e inténtalo de nuevo.' : 'Cargando puntos de agua…' })];
  const sel = el('select', { 'aria-label': 'Tipo de punto', onchange: e => { estado.grupo = e.target.value; dibujarPuntos(); } },
    ...[['todos', 'Todos'], ['hidrante', 'Hidrantes'], ['camara', 'Cámaras especiales']].map(([v, t]) => { const o = el('option', { value: v, text: t }); o.selected = estado.grupo === v; return o; }));
  const op = el('input', { type: 'checkbox', id: 'soloOp', onchange: e => { estado.soloOperativos = e.target.checked; dibujarPuntos(); } });
  op.checked = estado.soloOperativos;
  const lista = puntosFiltrados();
  const r = estado.referencia;
  const orden = r ? lista.slice().sort((a, b) => metros([r.lat, r.lon], [a.lat, a.lon]) - metros([r.lat, r.lon], [b.lat, b.lon])).slice(0, 15)
                  : lista.slice().sort((a, b) => a.nombre.localeCompare(b.nombre, 'es', { numeric: true }));
  return [el('div', { class: 'bloque acciones' }, sel, el('label', { for: 'soloOp' }, op, ' Solo puntos operativos')),
    el('p', { class: 'bloque pequeno suave', text: `${lista.filter(p => p.grupo === 'hidrante').length} Hidrantes · ${lista.filter(p => p.grupo === 'camara').length} Cámaras · Con estos filtros · Lima y Callao` }),
    r ? el('h2', { text: `Cerca de ${r.nombre}` }) : el('h2', { text: 'Todos los puntos' }),
    ...orden.slice(0, r ? 15 : 60).map(tarjetaPunto),
    !r && orden.length > 60 ? el('p', { class: 'bloque pequeno suave', text: 'Busca una dirección o usa «Mi ubicación» para ver los más cercanos.' }) : null];
}
function fichaPunto(p) {
  const color = p.situacion === 'Operativo' ? '#34c759' : p.situacion === 'En reparación' ? '#ff3b30' : '#ff9500';
  const r = estado.referencia, d = r ? metros([r.lat, r.lon], [p.lat, p.lon]) : null;
  const fila = (k, v) => v ? [el('dt', { text: k }), el('dd', {}, v)] : [];
  return [el('h2', { text: `${grupoNombre(p)} · ${p.nombre}` }),
    el('dl', { class: 'ficha' }, ...fila('Tipo', p.tipo), ...fila('Código', p.nombre), ...fila('Dirección', p.direccion),
      ...fila('Centro de servicio', p.centro), ...fila('Estado', el('span', {}, el('span', { class: 'muestra', style: `background:${color};border-radius:50%` }), p.situacion)),
      ...fila('Capacidad', p.capacidad > 0 ? String(Math.round(p.capacidad * 10) / 10).replace('.', ',') : '')),
    el('div', { class: 'bloque acciones' },
      el('strong', { text: 'Cómo llegar' }), d != null ? el('span', { class: 'suave', text: `${distancia(d)} · aprox. ${Math.max(1, Math.round(d * 1.3 / 75))} min a pie` }) : null,
      enlaceSeguro(`https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}&travelmode=walking`, 'A pie con Google Maps'),
      el('button', { class: 'enlace', type: 'button', text: 'Ver en el mapa', onclick: () => mapa.setView([p.lat, p.lon], 17) })),
    el('p', { class: 'bloque pequeno suave', text: 'Fuente: Sunass (GeoSunass). La información es referencial; ante una emergencia sigue las indicaciones de las autoridades y de tu empresa de agua (SEDAPAL).' })];
}

/* ---------- 4. aniegos recientes (feed propio) ---------- */
async function cargarAniegos() {
  try {
    const t = Math.floor(Date.now() / 120000);
    const [feed, sombra] = await Promise.all([fetch(RAW + 'aniegos.json?t=' + t).then(r => r.json()),
      fetch(RAW + 'gpt-sombra.json?t=' + t).then(r => r.json()).catch(() => null)]);
    estado.aniegos = feed; estado.sombra = sombra;
  } catch { estado.aniegos = estado.aniegos || { error: true, reports: [] }; }
  grupos.aniegos.clearLayers();
  for (const r of vigentesAniegos()) if (r.kind === 'incident' && Array.isArray(r.coordinates)) {
    L.circleMarker([r.coordinates[1], r.coordinates[0]], { radius: 9, color: '#fff', weight: 2, fillColor: '#0d66d1', fillOpacity: 0.95, pane: 'puntos' })
      .on('click', () => seleccionar([el('span', { class: 'chip azul', text: 'Incidente revisado' }), el('h3', { text: r.location }),
        el('p', { class: 'suave', text: `${r.district} · Reporte del ${fechaDia(r.date)}` }), el('p', { class: 'pequeno', text: r.precision })],
        () => fichaAniego(r))).addTo(grupos.aniegos);
  }
  if (estado.pestana === 'aniegos') pintarContenido();
}
const vigentesAniegos = () => (estado.aniegos?.reports || []).filter(r => new Date(r.expires_at) > Date.now());
const fechaDia = d => { const [a, m, x] = String(d).split('-'); return x ? `${x}/${m}/${a}` : 'Fecha no informada'; };
function fichaAniego(r) {
  const esPub = r.kind === 'publication';
  const ubicable = Array.isArray(r.coordinates);
  return [el('article', { class: 'item' + (ubicable ? '' : ' estatico'), tabindex: ubicable ? '0' : null,
      onclick: e => { if (ubicable && !e.target.closest('a,button')) mapa.setView([r.coordinates[1], r.coordinates[0]], 16); } },
    el('span', { class: 'chip' + (esPub ? '' : ' azul'), text: esPub ? 'Publicación por verificar' : 'Incidente revisado' }),
    el('h3', { text: esPub ? r.summary : r.location }), el('p', { class: 'suave', text: `${r.district} · ${esPub ? 'Publicada el ' : 'Reporte del '}${fechaDia(r.date)}` }),
    esPub ? null : el('p', { text: r.summary }), el('p', { class: 'pequeno suave', text: r.precision }),
    el('div', { class: 'acciones' },
      Array.isArray(r.coordinates) ? el('button', { class: 'enlace', type: 'button', text: 'Ver en el mapa', onclick: () => mapa.setView([r.coordinates[1], r.coordinates[0]], 16) }) : null,
      ...(r.sources || []).map(s => enlaceSeguro(s.url, s.name))))];
}
function listaAniegos() {
  if (!estado.aniegos) return [el('p', { class: 'bloque suave', text: 'Cargando avisos…' })];
  if (estado.aniegos.error) return [el('p', { class: 'bloque suave', text: 'No pudimos cargar los avisos. Inténtalo de nuevo en unos minutos.' })];
  const filas = vigentesAniegos();
  const viejo = (Date.now() - new Date(estado.aniegos.generated_at)) / 60000 > 90;
  const nodos = [el('p', { class: 'bloque aviso pequeno', text: 'Cobertura parcial: un mapa sin puntos no significa que no haya aniegos. Los puntos son referenciales.' + (viejo ? ' La recopilación lleva más de 90 minutos sin actualizarse.' : '') })];
  const porDistrito = new Map();
  for (const r of filas) { if (!porDistrito.has(r.district)) porDistrito.set(r.district, []); porDistrito.get(r.district).push(r); }
  for (const [d, rs] of [...porDistrito].sort((a, b) => b[1].length - a[1].length)) {
    nodos.push(el('h2', { text: `${d} · ${rs.length}` }), ...rs.flatMap(fichaAniego));
  }
  if (!filas.length) nodos.push(el('p', { class: 'bloque suave', text: 'No hay avisos vigentes en las últimas 48 horas. Eso no garantiza que no haya aniegos.' }));
  const sombra = estado.sombra?.current || [];
  if (sombra.length) nodos.push(el('details', { class: 'bloque' }, el('summary', { text: `Detección automática en prueba · ${sombra.length} (no publicada)` }),
    ...sombra.map(s => el('p', { class: 'pequeno', text: `${fechaDia(s.day)} · ${s.district} · ${s.location}${s.date_status !== 'stated' ? ' · fecha por verificar' : ''}` }))));
  nodos.push(el('p', { class: 'bloque pequeno suave' }, el('a', { href: 'costos.html', text: 'Costo de la exploración automática de noticias' })));
  return nodos;
}

/* ---------- 5. zonas inundables (MINAM, teselas) ---------- */
const MUNDO = 20037508.342789244;
const Inundables = L.TileLayer.extend({
  getTileUrl(c) {
    const lado = 2 * MUNDO / Math.pow(2, c.z);
    const bbox = [-MUNDO + c.x * lado, MUNDO - (c.y + 1) * lado, -MUNDO + (c.x + 1) * lado, MUNDO - c.y * lado].join(',');
    return `${MINAM}CS/Riesgos/MapServer/export?f=image&bbox=${bbox}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&layers=show:28`;
  },
});
let capaInundables = null, fallasTeselas = 0;
function activarInundables() {
  if (capaInundables) return;
  capaInundables = new Inundables('', { pane: 'inundables', opacity: 0.85, maxZoom: 19, minZoom: 10,
    bounds: L.latLngBounds([-12.543, -77.160], [-11.734, -76.593]),
    attribution: 'Zonas inundables T100 © <a href="' + MINAM + 'CS/Riesgos/MapServer/28">MINAM</a>' });
  capaInundables.on('tileerror', () => { fallasTeselas++; aviso('Cartografía incompleta. No indica ausencia de riesgo.'); });
  capaInundables.on('load', () => { if (!fallasTeselas) aviso(null); fallasTeselas = 0; });
  capaInundables.addTo(grupos.inundables);
}

/* ---------- 6. puntos críticos ANA (MINAM) ---------- */
let anaCargado = false;
async function cargarAna() {
  if (anaCargado) return;
  const base = { where: "ubi_dpto IN ('15','07')", geometry: '-77.3,-12.6,-76.4,-11.6', geometryType: 'esriGeometryEnvelope',
    inSR: '4326', spatialRel: 'esriSpatialRelIntersects', outSR: '4326',
    outFields: 'OBJECTID,paraje,nom_dist,nomb_rio_q,tip_peligr,fecha_actu,fuente,url', returnGeometry: 'true', f: 'json' };
  const url = p => `${MINAM}ServicioFocalizacionIN/MapServer/4/query?${new URLSearchParams(p)}`;
  try {
    const ids = await (await fetch(url({ ...base, returnIdsOnly: 'true' }))).json();
    if (ids.error || ids.objectIdFieldName !== 'OBJECTID' || !Array.isArray(ids.objectIds) || ids.objectIds.length > 2000) throw 0;
    const lista = [...new Set(ids.objectIds)].filter(n => n > 0).sort((a, b) => a - b);
    const puntos = [];
    for (let i = 0; i < lista.length; i += 100) {
      const lote = lista.slice(i, i + 100);
      const d = await (await fetch(url({ ...base, objectIds: lote.join(',') }))).json();
      if (d.error || d.geometryType !== 'esriGeometryPoint' || d.exceededTransferLimit) throw 0;
      for (const f of d.features || []) {
        const { x, y } = f.geometry || {};
        if (!(x >= -77.3 && x <= -76.4 && y >= -12.6 && y <= -11.6)) continue;
        puntos.push({ ...f.attributes, x, y });
      }
    }
    for (const p of puntos) {
      const anio = limpio(p.fecha_actu, 20) || 'Sin año informado';
      const enlace = enlaceSeguro(p.url, 'Ver ficha oficial', ['sigrid.cenepred.gob.pe', 'www.ana.gob.pe', 'ana.gob.pe', 'geoservidorperu.minam.gob.pe']);
      const cont = el('div', {}, el('strong', { text: limpio(p.paraje, 120) || 'Punto crítico' }),
        el('div', { text: `ANA · Inventario ${anio}${p.nomb_rio_q ? ' · ' + limpio(p.nomb_rio_q, 80) : ''}` }),
        p.nom_dist ? el('div', { text: titulo(p.nom_dist) }) : null,
        el('div', { class: 'pequeno', text: 'Antecedente histórico, no un evento actual.' }), enlace);
      L.marker([p.y, p.x], { pane: 'puntos', icon: L.divIcon({ className: '', html: '<div class="ana-icono">!</div>', iconSize: [22, 22] }) })
        .bindPopup(cont).addTo(grupos.ana);
    }
    anaCargado = true;
  } catch { aviso('No pudimos cargar los puntos ANA. La capa puede estar incompleta.'); }
}

/* ---------- 7. riesgo por lluvias (CENEPRED, recopilado por el Action) ---------- */
const RIESGOS = [{ nivel: 'Muy alto', color: '#ff3b30' }, { nivel: 'Alto', color: '#ff9500' },
                 { nivel: 'Medio', color: '#ffcc00' }, { nivel: 'Bajo', color: '#34c759' }];
const colorRiesgo = n => (RIESGOS.find(r => r.nivel === n) || { color: '#8e8e93' }).color;
const lluvias = { indice: null, error: false, elegido: null, datos: {}, cargando: false, distrito: null, capa: null };
async function cargarLluvias() {
  if (!lluvias.indice) {
    try {
      lluvias.indice = await (await fetch(RAW + 'riesgo-lluvias.json?t=' + Math.floor(Date.now() / 120000))).json();
      const guardado = leer('lluvias_escenario')?.valor;
      const conMapa = lluvias.indice.escenarios.filter(e => e.distritos);
      const lima = e => (e.departamentos || []).some(d => d === 'LIMA' || d === 'CALLAO');
      lluvias.elegido = (conMapa.find(e => e.id === guardado) || conMapa.find(e => e.estado === 'vigente' && lima(e)) ||
                         conMapa.find(e => e.estado === 'vigente') || conMapa[0])?.id || null;
    } catch { lluvias.error = true; }
  }
  await dibujarLluvias();
}
async function dibujarLluvias(encuadrar) {
  const id = lluvias.elegido;
  if (id && !lluvias.datos[id]) {
    lluvias.cargando = true; if (estado.pestana === 'lluvias') pintarContenido();
    try { lluvias.datos[id] = await (await fetch(RAW + 'riesgo-lluvias/' + id + '.json')).json(); }
    catch { lluvias.datos[id] = null; aviso('No pudimos cargar el mapa distrital de este escenario. Esto no significa que no exista riesgo.'); }
    lluvias.cargando = false;
  }
  grupos.lluvias.clearLayers();
  const fc = id && lluvias.datos[id];
  if (fc) {
    const estilo = f => { const sel = f.id === lluvias.distrito?.ubigeo, c = colorRiesgo(f.properties.nivel);
      return { color: sel ? css('--texto') : c, weight: sel ? 2.5 : 1, fillColor: c, fillOpacity: sel ? 0.48 : 0.24 }; };
    lluvias.capa = L.geoJSON(fc, { pane: 'lluvias', renderer: lienzos.lluvias, smoothFactor: 0.5, style: estilo,
      onEachFeature: (f, capa) => capa.on('click', e => { L.DomEvent.stop(e); lluvias.distrito = f.properties; lluvias.capa.setStyle(estilo);
        const p = f.properties;
        seleccionar([el('span', { class: 'chip', style: `background:${colorRiesgo(p.nivel)};color:#111;border-color:transparent`, text: 'Riesgo ' + p.nivel.toLowerCase() }),
          el('h3', { text: titulo(p.distrito) }), el('p', { class: 'suave', text: [titulo(p.provincia), titulo(p.departamento)].filter(Boolean).join(' · ') }),
          el('p', { class: 'pequeno', text: 'Escenario por distrito, no aniegos actuales.' })], () => fichaDistrito(p)); }) }).addTo(grupos.lluvias);
    if (encuadrar) mapa.fitBounds(lluvias.capa.getBounds(), { padding: [30, 30] });
  }
  if (estado.pestana === 'lluvias') pintarContenido();
}
function vigencia(e) {
  const fin = new Date(new Date(e.fin).getTime() - 1000);
  return `${capital(dia(e.inicio))} – ${dia(fin)}. Hora de Perú.`;
}
const escenarioActual = () => lluvias.indice?.escenarios.find(e => e.id === lluvias.elegido);
function fichaDistrito(p) {
  const e = escenarioActual();
  return [el('div', { class: 'bloque' },
    el('span', { class: 'chip', style: `background:${colorRiesgo(p.nivel)};color:#1d1600;border-color:transparent`, text: 'Riesgo ' + p.nivel.toLowerCase() }),
    el('h2', { text: titulo(p.distrito), style: 'margin:8px 0 2px' }),
    el('p', { class: 'suave', text: [titulo(p.provincia), titulo(p.departamento)].filter(Boolean).join(' · ') + ' · UBIGEO ' + p.ubigeo }),
    e ? el('p', { class: 'pequeno', text: `Aviso ${e.aviso} · ${capital(e.ambito)}. ${vigencia(e)}` }) : null,
    el('p', { class: 'pequeno', text: 'Escenario por distrito, no aniegos actuales. Los colores describen riesgo preventivo del ámbito distrital; no indican calles inundadas ni confirman una emergencia. Un nivel bajo tampoco significa ausencia de peligro.' }),
    e ? enlaceSeguro(e.documento, 'Informe oficial ↗', ['sigrid.cenepred.gob.pe']) : null)];
}
function listaLluvias() {
  if (lluvias.error) return [el('p', { class: 'bloque suave', text: 'No pudimos consultar los escenarios. Reintenta más tarde o abre la fuente oficial.' }),
    el('p', { class: 'bloque' }, enlaceSeguro('https://sigrid.cenepred.gob.pe/sigridv3/escenarios/lbam', 'Abrir SIGRID / CENEPRED', ['sigrid.cenepred.gob.pe']))];
  if (!lluvias.indice) return [el('p', { class: 'bloque suave', text: activa('lluvias') ? 'Consultando avisos…' : 'Activa la capa «Riesgo por lluvias» para ver los escenarios.' }),
    activa('lluvias') ? null : el('div', { class: 'bloque' }, el('button', { class: 'boton', type: 'button', text: 'Activar riesgo por lluvias',
      onclick: () => { guardar('capa_lluvias', true); $('capa-lluvias').checked = true; aplicarCapa('lluvias'); } }))];
  const ind = lluvias.indice, e = escenarioActual();
  const viejo = (Date.now() - new Date(ind.checked_at || ind.generated_at)) / 36e5 > 24;
  const nodos = [el('p', { class: 'bloque aviso pequeno', text: 'Escenario por distrito, no aniegos actuales.' +
    (viejo ? ' Copia descargada hace más de un día. Consulta el informe oficial.' : '') + (ind.error_lista ? ' No pudimos actualizar las publicaciones; los datos guardados pueden haber cambiado.' : '') })];
  nodos.push(el('h2', { text: 'Escenarios por lluvias' }));
  for (const x of ind.escenarios) {
    const sel = x.id === lluvias.elegido;
    nodos.push(el('article', { class: 'item estatico' + (sel ? ' destacado' : '') },
      el('span', { class: 'chip ' + (x.estado === 'vigente' ? 'naranja' : x.estado === 'proximo' ? 'acento' : 'gris'),
                   text: x.estado === 'vigente' ? 'Vigente' : x.estado === 'proximo' ? 'Próximo' : 'Vencido' }),
      el('h3', { text: `Aviso ${x.aviso} · ${capital(x.ambito)}` }), el('p', { class: 'pequeno', text: vigencia(x) }),
      x.departamentos ? el('p', { class: 'pequeno', text: (x.departamentos.some(d => d === 'LIMA' || d === 'CALLAO') ? 'Incluye Lima y Callao · ' : '') +
        x.departamentos.map(titulo).join(', ') }) : null,
      x.distritos ? el('p', { class: 'pequeno suave', text: `${x.distritos} distritos · ` + RIESGOS.filter(r => x.niveles?.[r.nivel]).map(r => `${r.nivel} ${x.niveles[r.nivel]}`).join(' · ') })
                  : el('p', { class: 'pequeno', text: x.error === 'sin_mapa' ? 'La fuente no tiene un mapa distrital disponible para este escenario. Esto no significa que no exista riesgo.' : 'No pudimos descargar el mapa de este escenario. Abre el informe oficial.' }),
      el('div', { class: 'acciones' },
        x.distritos ? el('button', { class: 'enlace', type: 'button', text: sel ? 'Encuadrar distritos' : 'Ver en el mapa', onclick: () => {
          lluvias.elegido = x.id; lluvias.distrito = null; guardar('lluvias_escenario', x.id);
          if (!activa('lluvias')) { guardar('capa_lluvias', true); $('capa-lluvias').checked = true; grupos.lluvias.addTo(mapa); }
          dibujarLluvias(true); } }) : null,
        enlaceSeguro(x.documento, 'Informe oficial ↗', ['sigrid.cenepred.gob.pe']))));
  }
  if (lluvias.cargando) nodos.push(el('p', { class: 'bloque suave', text: 'Cargando distritos…' }));
  nodos.push(el('p', { class: 'bloque pequeno suave', text: `CENEPRED / SIGRID · Copia del ${fechaHora(ind.generated_at)}. Toca un distrito para consultar su nivel.` +
    (ind.omitidos ? ` Se omitieron ${ind.omitidos} publicaciones con datos incoherentes.` : '') }),
    el('p', { class: 'bloque' }, enlaceSeguro(ind.lista, 'Ver todas las publicaciones en SIGRID', ['sigrid.cenepred.gob.pe'])));
  return nodos;
}

/* ---------- panel ---------- */
const ICONOS = {
  cortes: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><defs><mask id="mascaraGota"><rect width="24" height="24" fill="#fff"/>' +
    '<line x1="3.5" y1="3.5" x2="20.5" y2="20.5" stroke="#000" stroke-width="5"/></mask></defs>' +
    '<path fill="currentColor" mask="url(#mascaraGota)" d="M12 2.5c-3.3 4.4-6.5 8.2-6.5 11.6A6.5 6.5 0 0 0 12 20.6a6.5 6.5 0 0 0 6.5-6.5C18.5 10.7 15.3 6.9 12 2.5z"/>' +
    '<line x1="3.5" y1="3.5" x2="20.5" y2="20.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  puntos: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M12 2a7 7 0 0 1 7 7c0 5-7 13-7 13S5 14 5 9a7 7 0 0 1 7-7zm0 4.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"/></svg>',
  aniegos: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M2 15c2 0 2-1.5 4-1.5S8 15 10 15s2-1.5 4-1.5 2 1.5 4 1.5 2-1.5 4-1.5V16c-2 0-2 1.5-4 1.5S16 16 14 16s-2 1.5-4 1.5S8 16 6 16s-2 1.5-4 1.5zm0 4c2 0 2-1.5 4-1.5S8 19 10 19s2-1.5 4-1.5 2 1.5 4 1.5 2-1.5 4-1.5V20c-2 0-2 1.5-4 1.5S16 20 14 20s-2 1.5-4 1.5S8 20 6 20s-2 1.5-4 1.5zM12 2c-2 2.8-4 5.2-4 7.3a4 4 0 0 0 8 0C16 7.2 14 4.8 12 2z"/></svg>',
  lluvias: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7 15a5 5 0 0 1-.4-10A6 6 0 0 1 18 7a4 4 0 0 1-1 8H7zm1 2h2l-1.5 4h-2L8 17zm5 0h2l-1.5 4h-2L13 17z"/></svg>',
  inundables: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3 4h18v10H3V4zm2 2v6h14V6H5zm-3 11c2 0 2-1.5 4-1.5S8 17 10 17s2-1.5 4-1.5 2 1.5 4 1.5 2-1.5 4-1.5V18c-2 0-2 1.5-4 1.5S16 18 14 18s-2 1.5-4 1.5S8 18 6 18s-2 1.5-4 1.5z"/></svg>',
};
const PESTANAS = [['cortes', 'Cortes'], ['puntos', 'Puntos'], ['aniegos', 'Aniegos'], ['lluvias', 'Lluvias'], ['inundables', 'Inundables']];
function cuenta(id) {
  if (id === 'cortes' && estado.cortes) {
    const r = estado.referencia;
    return estado.cortes.filter(c => estadoCorte(c) !== 'vencido' && (r ? distanciaA(c.poligonos, r.lat, r.lon) <= 3000 :
      estado.zonaCortes !== 'lima' || c.departamentos.some(d => /^(LIMA|CALLAO)$/i.test(d)))).length;
  }
  if (id === 'aniegos' && estado.aniegos) return vigentesAniegos().length;
  if (id === 'lluvias' && lluvias.indice) return lluvias.indice.escenarios.filter(e => e.estado === 'vigente').length;
  return null;
}
function pintarPestanas() {
  $('pestanas').replaceChildren(...PESTANAS.map(([id, t]) => {
    const n = cuenta(id), b = el('button', { type: 'button', role: 'tab', 'aria-selected': String(estado.pestana === id && !estado.detalle),
      onclick: () => { estado.pestana = id; estado.detalle = null; pintarPestanas(); pintarContenido(); $('panel').scrollTop = 0; } });
    b.innerHTML = ICONOS[id];  // constant SVG only
    b.append(t, n != null ? el('span', { class: 'cuenta', text: String(n) }) : '');
    return b;
  }));
}
// Selecting anything on the map shows a short card over the map and the full detail in the panel.
let alCerrarSeleccion = null;
function seleccionar(resumen, detalle, alCerrar) {
  alCerrarSeleccion?.(); alCerrarSeleccion = alCerrar || null;
  const t = $('tarjetaMapa');
  const cerrar = el('button', { type: 'button', class: 'cerrar', 'aria-label': 'Cerrar', text: '×', onclick: cerrarSeleccion });
  const acciones = detalle ? el('div', { class: 'acciones solo-movil' }, el('button', { class: 'boton primario pequeno', type: 'button', text: 'Ver detalle',
    onclick: () => { abrir(detalle); if (matchMedia('(max-width: 760px)').matches) $('panel').scrollIntoView({ behavior: 'smooth', block: 'start' }); } })) : null;
  t.replaceChildren(...[el('div', { class: 'cabecera' }, el('div', {}, ...resumen), cerrar), acciones].filter(Boolean));
  t.hidden = false;
  $('popoverCapas').hidden = true; $('botonCapas').setAttribute('aria-expanded', 'false');
  if (detalle && !matchMedia('(max-width: 760px)').matches) abrir(detalle);
}
function cerrarSeleccion() {
  $('tarjetaMapa').hidden = true;
  alCerrarSeleccion?.(); alCerrarSeleccion = null;
  resaltarCorte(null);
  if (sectorElegido) { sectorElegido = null; capaSectores?.setStyle(estiloSector); }
  if (lluvias.distrito) { lluvias.distrito = null; lluvias.capa?.setStyle(lluvias.capa.options.style); }
  if (estado.detalle) { estado.detalle = null; pintarPestanas(); pintarContenido(); }
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') cerrarSeleccion(); });
mapa.on('click', () => { if (!$('tarjetaMapa').hidden) cerrarSeleccion(); });
function abrir(dibujar) { estado.detalle = dibujar; pintarPestanas(); pintarContenido(); $('panel').scrollTop = 0; }
function pintarContenido() {
  const c = $('contenido');
  if (estado.detalle) {
    c.replaceChildren(el('div', { class: 'bloque' }, el('button', { class: 'enlace volver', type: 'button', text: '← Volver a la lista',
      onclick: cerrarSeleccion })), ...estado.detalle());
    return;
  }
  const ref = estado.referencia ? [el('div', { class: 'bloque' }, el('div', { class: 'aviso pequeno' },
    el('strong', { text: 'Consultando: ' }), estado.referencia.nombre, ' · ',
    el('button', { class: 'enlace', type: 'button', text: 'Quitar', onclick: quitarReferencia })))] : [];
  const vista = { cortes: listaCortes, puntos: listaPuntos, aniegos: listaAniegos, lluvias: listaLluvias, inundables: infoInundables }[estado.pestana];
  c.replaceChildren(...ref, ...vista());
}
function infoInundables() {
  return [el('div', { class: 'bloque' },
    el('h2', { text: 'Zonas inundables · Lima y Callao', style: 'margin:6px 0' }),
    el('p', { class: 'pequeno', text: 'La capa oficial de superficies inundables T100 de Lima y Callao publicada por el MINAM. T100 representa un período de retorno de 100 años en el modelo; no significa que ocurrirá dentro de 100 años ni es un aviso de lluvias.' }),
    el('p', { class: 'pequeno', text: 'Sin color no garantiza seguridad: puede faltar cobertura o información. El zoom no aporta precisión predial. La fecha del estudio T100 no está informada por el servicio.' }),
    el('p', { class: 'pequeno', text: 'El inventario ANA muestra puntos históricos con el año individual. No equivale a un reporte de emergencias en curso.' }),
    el('div', { class: 'acciones' },
      el('button', { class: 'boton', type: 'button', text: activa('inundables') ? 'Capa activada' : 'Activar zonas inundables',
        onclick: () => { guardar('capa_inundables', true); $('capa-inundables').checked = true; aplicarCapa('inundables'); mapa.setView([-12.02, -77.02], 13); pintarContenido(); } }),
      enlaceSeguro(MINAM + 'CS/Riesgos/MapServer/28', 'Ver capa oficial MINAM ↗'),
      el('a', { href: 'pasos.html', text: 'Pasos a desnivel · CENEPRED' })))];
}

/* ---------- búsqueda y ubicación ---------- */
let marcaReferencia = null;
function fijarReferencia(ref, zoom = 15) {
  estado.referencia = ref; estado.detalle = null;
  marcaReferencia?.remove();
  marcaReferencia = L.circleMarker([ref.lat, ref.lon], { radius: 8, color: '#fff', weight: 3, fillColor: '#14181f', fillOpacity: 1, pane: 'puntos' })
    .bindTooltip(ref.tipo === 'ubicacion' ? 'Tu ubicación' : 'Dirección buscada').addTo(mapa);
  mapa.setView([ref.lat, ref.lon], zoom);
  pintarPestanas(); pintarContenido();
}
function quitarReferencia() { estado.referencia = null; marcaReferencia?.remove(); marcaReferencia = null; pintarContenido(); }
let ultimaBusqueda = 0;
$('buscador').addEventListener('submit', async e => {
  e.preventDefault();
  const texto = $('consulta').value.trim(), caja = $('resultados');
  if (texto.length < 3) return;
  if (Date.now() - ultimaBusqueda < 1100) return; // política de uso de Nominatim: 1 consulta por segundo
  ultimaBusqueda = Date.now();
  caja.hidden = false; caja.replaceChildren(el('p', { class: 'suave', text: 'Buscando…' }));
  try {
    const q = new URLSearchParams({ q: texto, format: 'jsonv2', limit: '6', countrycodes: 'pe', 'accept-language': 'es',
      viewbox: '-77.4,-11.5,-76.45,-12.65', bounded: '1' });
    const r = await (await fetch('https://nominatim.openstreetmap.org/search?' + q)).json();
    const ok = r.filter(x => +x.lat >= -12.65 && +x.lat <= -11.5 && +x.lon >= -77.4 && +x.lon <= -76.45);
    if (!ok.length) { caja.replaceChildren(el('p', { text: 'No encontramos ese lugar en Lima y Callao. Prueba con la calle y el distrito.' })); return; }
    caja.replaceChildren(el('p', { class: 'pequeno suave', text: 'Elige una ubicación. La búsqueda no determina si la calle está en riesgo.' }),
      ...ok.map(x => el('button', { type: 'button', text: x.display_name, onclick: () => {
        caja.hidden = true;
        fijarReferencia({ lat: +x.lat, lon: +x.lon, nombre: limpio(x.name || x.display_name.split(',')[0], 80), tipo: 'direccion' }, 16);
      } })), el('p', { class: 'pequeno suave', text: 'Búsqueda: © OpenStreetMap (Nominatim)' }));
  } catch { caja.replaceChildren(el('p', { text: 'No pudimos buscar ese lugar. Revisa la conexión y reintenta.' })); }
});
$('miUbicacion').addEventListener('click', () => {
  if (!navigator.geolocation) return aviso('Este navegador no permite obtener tu ubicación.');
  aviso('Buscando tu ubicación…');
  navigator.geolocation.getCurrentPosition(
    p => { aviso(null); fijarReferencia({ lat: p.coords.latitude, lon: p.coords.longitude, nombre: 'tu ubicación', tipo: 'ubicacion' }, 15); },
    () => aviso('Permite el acceso a tu ubicación en el navegador y vuelve a pulsar el botón.'),
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
});

/* ---------- inicio ---------- */
pintarCapas();
for (const c of CAPAS) aplicarCapa(c.id);
const params = new URLSearchParams(location.search);
const pLat = parseFloat(params.get('lat')), pLon = parseFloat(params.get('lon'));
if (pLat >= -12.65 && pLat <= -11.5 && pLon >= -77.4 && pLon <= -76.45) {
  if (params.get('capa') === 'inundables') { guardar('capa_inundables', true); $('capa-inundables').checked = true; aplicarCapa('inundables'); estado.pestana = 'inundables'; }
  fijarReferencia({ lat: pLat, lon: pLon, nombre: limpio(params.get('nombre') || 'el lugar elegido', 80), tipo: 'direccion' }, 16);
}
pintarPestanas(); pintarContenido();
setInterval(() => { dibujarCortes(); }, 60000);        // estados por hora, sin volver a descargar
setInterval(() => { if (activa('cortes')) cargarCortes(); if (activa('aniegos')) cargarAniegos(); }, 15 * 60000);
