# Reportes recientes de aniegos para Agua Cerca

Recopilación pública con Python 3.9+, sin claves ni dependencias. La app descarga `data/aniegos.json` y vuelve a filtrar los registros vencidos.

```sh
python3 -m unittest discover -s tools/aniegos/tests -v
python3 tools/aniegos/generate.py --output /tmp/aniegos-resultado
python3 tools/aniegos/publish.py --report /tmp/aniegos-resultado/report.json --output data/aniegos.json --places-cache data/lugares-osm.json
```

## Qué se publica

- `incident`: registro revisado en `reviewed.json`, con resumen propio, distrito, lugar publicado, fecha del reporte y fuentes. No se afirma que siga activo. Solo se dibuja si tiene coordenadas referenciales documentadas; nunca se dibuja un polígono de extensión inundada.
- `publication`: artículo descubierto o descargado con fecha reciente, por verificar. La fecha es de publicación, no de ocurrencia. No genera un marcador. La app permite leer el aviso y abrir la fuente.

Los nuevos artículos no se convierten automáticamente en incidentes georreferenciados. La revisión separa los lugares de una misma nota, excluye antecedentes antiguos y documenta la precisión. Las fuentes ya asociadas a un incidente revisado no lo renuevan al vencer. Los estados de atención/resolución requieren una publicación de seguimiento; en esta versión todos los incidentes se muestran como situación actual sin confirmar.

## Ubicación automática con OpenStreetMap

`publish.py --places-cache data/lugares-osm.json` intenta ubicar cada incidente revisado y vigente que no tenga coordenadas. Las reglas están en `osm_places.py`, son deterministas y no usan IA:

- **Cruce:** el lugar nombra dos vías («Av. X con Av. Y»). Ambas deben existir en OSM con el mismo nombre y compartir nodos que formen un solo cruce dentro del polígono del distrito escrito en la revisión. Si hay dos cruces posibles, no se dibuja nada.
- **Bypass, paso a desnivel, puente o túnel:** si el texto lo menciona, el punto se mueve al centro de las estructuras viales cortas junto a ese cruce, que es donde se acumula el agua. Se descartan viaductos largos y puentes peatonales.
- **Grupo residencial:** «Sector N, Grupo M», con un solo lugar de ese nombre exacto en OSM dentro del distrito. El punto es el centro del grupo (±150 m), no la calle exacta.
- **Establecimiento:** hospital, clínica, mercado o colegio nombrado en la revisión, con una sola coincidencia dentro del distrito. El punto es referencial: no es el lugar exacto del agua.
- Una sola avenida, una cuadra, un sector sin grupo o un distrito «por precisar» no generan punto.

El resultado pasa por `validate_geocoding` y se marca como punto referencial de ±50 m, con atribución a OpenStreetMap (ODbL). La caché guarda los aciertos 30 días y los casos sin coincidencia 24 h. Un fallo de Nominatim u Overpass no se guarda: se reintenta en la siguiente ejecución y nunca equivale a «no existe el lugar». Por ejecución hay como máximo seis búsquedas nuevas y 150 s de red. Las coordenadas escritas a mano en `reviewed.json` tienen prioridad.

## Fuentes y límites

Se consultan RSS de Defensoría, Caretas, Infobae, La República, El Búho, Andina y El Comercio, además del descubrimiento por Google News y URLs conocidas de prueba de RPP y América. Un RSS puede no contener todas las noticias. Los enlaces Google son descubrimiento, no prueba del incidente; no se decodifican usando APIs privadas. Las fechas de los artículos se validan con sus metadatos o el RSS del propio publicador. No se toman coordenadas de la dirección de la redacción del medio.

La ventana máxima es 48 h. Las fechas sin hora se retiran conservadoramente al cumplirse 48 h desde el inicio del día publicado. Se usa America/Lima y se rechazan fechas futuras. Cada ejecución genera un reporte de errores; un fallo total no sobrescribe el archivo público. El archivo anterior puede quedar disponible, pero la app oculta sus registros vencidos y avisa cuando la recopilación no se actualiza durante más de 90 minutos.

Los cuerpos de artículos se procesan en memoria y no se publican. Se conservan enlaces, metadatos, resúmenes propios y títulos de publicaciones pendientes. HTTPS con hosts permitidos también en redirecciones, límite de 3 MB por respuesta, máximo 30 artículos, espera por host y timeout. No se eluden bloqueos ni se reintenta un HTTP 429.

Algunos medios y CDN envían páginas comprimidas con gzip aunque no se pida; el recolector las descomprime con el mismo límite de 3 MB. Con `--rss-cache data/rss-vistos.json` se recuerda la fecha que dio el RSS del propio medio (nunca la de Google News) para seguir leyendo una nota durante 48 h aunque haya salido del RSS. No se guardan cuerpos de notas. Se leen hasta 30 artículos por ejecución.

## Operación

El workflow ejecuta pruebas, recopila, genera el JSON y publica solo `data/aniegos.json` en este repositorio. Programado cada 30 minutos (minutos 17 y 47 UTC) y manualmente. GitHub puede retrasar ejecuciones: no es tiempo real. Los artifacts de diagnóstico se conservan siete días. Los cambios del catálogo revisado requieren editar `reviewed.json`; no contienen direcciones privadas de afectados ni cuerpos de noticias.

El GeoJSON del primer piloto sigue siendo un diagnóstico de su antiguo contrato de revisión; la app utiliza exclusivamente `data/aniegos.json`, que admite avisos sin coordenadas. Cobertura parcial: un mapa sin puntos no garantiza ausencia de aniegos.
