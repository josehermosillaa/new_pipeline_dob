# Pipeline DOB NOW reanudable para dos PCs Linux

Este directorio contiene un pipeline nuevo y autónomo. No reemplaza, modifica ni importa los scripts existentes de `nuevo/`.

Puede copiarse `nuevo_pipeline/` a las dos PCs junto con el CSV. No depende de `nuevo/patchright_hybrid.py`, `vpn.py`, los checkpoints anteriores ni las cachés JSON anteriores.

## Objetivo

El pipeline:

1. lee el CSV original sin agrupar;
2. clasifica cada filing como prioridad A, B o C;
3. agrupa los filings seleccionados por BIN;
4. divide los BIN completos entre dos PCs sin solaparlos;
5. obtiene los GUID una sola vez por BIN;
6. guarda independientemente PW1, ZD1WD y Portal Documents;
7. une y deduplica documentos por `DocumentURL`;
8. solicita una URL de descarga por documento objetivo único;
9. conserva el progreso en SQLite;
10. exporta exactamente las 24 columnas de los CSV actuales.

La estrategia y su justificación estadística están en [`../nuevo/ESTRATEGIA_PROCESAMIENTO.md`](../nuevo/ESTRATEGIA_PROCESAMIENTO.md).

## Archivos

| Archivo | Función |
|---|---|
| `prepare_inputs.py` | Clasifica, agrupa, divide y crea la base de una PC |
| `worker.py` | Ejecuta búsquedas y endpoints reanudables |
| `dobnow_client.py` | Chrome, Angular y llamadas DOB NOW autónomas |
| `database.py` | Esquema y utilidades SQLite |
| `export_results.py` | Exporta una base al CSV compatible |
| `merge_results.py` | Consolida los CSV de ambas PCs |
| `requirements.txt` | Dependencias Python |
| `systemd/dobnow-worker.service.example` | Servicio Linux de ejemplo |
| `tests/test_pipeline.py` | Pruebas de partición y contrato CSV |

## SQLite

No se instala SQLite por separado. Python incluye el módulo estándar `sqlite3`.

Verificación:

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Cada PC usa una base local diferente. No se debe abrir la misma base desde ambas PCs ni sincronizar una base activa mediante OneDrive, Dropbox o una carpeta de red.

## Instalación en cada PC

Los ejemplos suponen que el proyecto está en:

```text
/opt/dobnow/scraper_sec
```

Adapta la ruta a la ubicación real.

```bash
cd /opt/dobnow/scraper_sec
python3 -m venv /opt/dobnow/venv
source /opt/dobnow/venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r nuevo_pipeline/requirements.txt
```

El pipeline sólo requiere Patchright, Chrome y la biblioteca estándar de Python. No utiliza `curl_cffi`, ProtonVPN ni módulos del scraper anterior.

Si la instalación de Patchright de tu entorno requiere instalar el navegador administrado:

```bash
patchright install chrome
```

Comprueba las dependencias:

```bash
python -c "import sqlite3, patchright; print('dependencias OK')"
```

Para ejecución sin escritorio gráfico puede utilizarse Xvfb:

```bash
sudo apt-get install xvfb
```

## Preparar el mismo CSV en las dos PCs

Las dos máquinas deben tener una copia idéntica de:

```text
filter_gc_20260721.csv
```

Comprueba que el hash sea igual:

```bash
sha256sum filter_gc_20260721.csv
```

### PC 1: mitad superior

```bash
cd /opt/dobnow/scraper_sec
source /opt/dobnow/venv/bin/activate

python nuevo_pipeline/prepare_inputs.py \
  --input /opt/dobnow/data/filter_gc_20260721.csv \
  --partition 1/2 \
  --priorities A,B \
  --db /opt/dobnow/data/state_pc1.sqlite
```

### PC 2: mitad inferior

```bash
cd /opt/dobnow/scraper_sec
source /opt/dobnow/venv/bin/activate

python nuevo_pipeline/prepare_inputs.py \
  --input /opt/dobnow/data/filter_gc_20260721.csv \
  --partition 2/2 \
  --priorities A,B \
  --db /opt/dobnow/data/state_pc2.sqlite
```

Ambas PCs calculan la misma clasificación y el mismo punto de corte. La partición ocurre después de agrupar, por lo que un BIN completo pertenece a una única PC.

La división es contigua: PC 1 recibe la parte superior y PC 2 la inferior. El corte se aproxima al 50% de la carga estimada, no necesariamente al 50% exacto del número de BIN.

> **Advertencia:** `--force` elimina y reconstruye la base indicada. No lo uses después de comenzar el procesamiento, salvo que quieras descartar todo el progreso de esa PC.

## Prioridades

La base puede contener A y B simultáneamente. El worker permite decidir qué ejecutar.

Sólo A:

```bash
python nuevo_pipeline/worker.py --db /opt/dobnow/data/state_pc1.sqlite --priorities A
```

Sólo B:

```bash
python nuevo_pipeline/worker.py --db /opt/dobnow/data/state_pc1.sqlite --priorities B
```

A primero y luego B automáticamente:

```bash
python nuevo_pipeline/worker.py --db /opt/dobnow/data/state_pc1.sqlite --priorities A,B
```

El orden interno siempre coloca A antes de B.

## Primera prueba

Antes de dejar el worker funcionando continuamente, ejecuta diez tareas:

### PC 1

```bash
xvfb-run -a /opt/dobnow/venv/bin/python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc1 \
  --priorities A \
  --max-tasks 10 \
  --pause-min 6 \
  --pause-max 15
```

### PC 2

```bash
xvfb-run -a /opt/dobnow/venv/bin/python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc2.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc2 \
  --priorities A \
  --max-tasks 10 \
  --pause-min 6 \
  --pause-max 15
```

Si ya administras Chrome mediante CDP, usa el perfil y proceso Chrome existentes:

```bash
python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --cdp-port 9222 \
  --priorities A \
  --max-tasks 10
```

No uses simultáneamente dos workers sobre la misma base y el mismo perfil de navegador.

## Ejecución normal

PC 1:

```bash
xvfb-run -a /opt/dobnow/venv/bin/python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc1 \
  --priorities A \
  --pause-min 6 \
  --pause-max 15
```

PC 2:

```bash
xvfb-run -a /opt/dobnow/venv/bin/python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc2.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc2 \
  --priorities A \
  --pause-min 6 \
  --pause-max 15
```

Cuando A termine, cambia `--priorities A` por `--priorities B`. No vuelvas a ejecutar `prepare_inputs.py`: los trabajos A completados permanecen en SQLite.

## Bloqueos y recuperación

El worker no borra cookies, no limpia el perfil y no rota VPN.

Cuando detecta `Access Denied` o un bloqueo explícito:

1. guarda la tarea exacta como `retry`;
2. conserva endpoints ya completados;
3. cierra de manera controlada;
4. incrementa un contador persistente de bloqueos;
5. devuelve código de salida `3` mientras está en `COOLDOWN`.

Con `--block-threshold 3`, después del tercer bloqueo consecutivo:

1. la sesión pasa a estado `NEEDS_SESSION`;
2. se crea un archivo `NEEDS_SESSION` junto a la base;
3. el worker termina con código `4`;
4. el servicio systemd deja de reiniciarlo automáticamente;
5. se requiere recuperar y validar manualmente el mismo perfil Chrome.

El marcador contiene fecha, base, perfil, número de bloqueos y confirmación de que el progreso fue guardado. No guarda cookies, tokens ni URLs de documentos.

### Recuperar manualmente la sesión

Ejemplo para PC 1.

1. Detener el servicio:

```bash
sudo systemctl stop dobnow-worker.service
```

2. Confirmar el marcador:

```bash
cat /opt/dobnow/data/NEEDS_SESSION
```

3. Abrir Chrome manualmente con **el mismo perfil** usado por el worker:

```bash
google-chrome \
  --user-data-dir=/opt/dobnow/data/chrome_profile_pc1
```

4. Realizar manualmente el procedimiento de recuperación que corresponda y comprobar que DOB NOW abre sin `Access Denied`.

5. Cerrar completamente ese Chrome. No debe quedar abierto mientras Patchright intenta utilizar el mismo perfil.

6. Validar la sesión desde el pipeline:

```bash
cd /opt/dobnow/scraper_sec
source /opt/dobnow/venv/bin/activate

xvfb-run -a python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc1 \
  --priorities A \
  --check-session \
  --log-file /opt/dobnow/data/worker_pc1.log
```

Si la validación es exitosa:

- se elimina `NEEDS_SESSION`;
- el contador de bloqueos vuelve a cero;
- el estado de sesión cambia a `HEALTHY`;
- no se procesa ningún BIN durante esta comprobación.

7. Reiniciar el servicio:

```bash
sudo systemctl start dobnow-worker.service
```

Al iniciarlo nuevamente continuará desde el endpoint pendiente. No repetirá BIN, PW1 o endpoints que ya estén marcados como completados.

Los errores transitorios normales reciben `--retry-delay` segundos antes de ser elegibles otra vez. El valor predeterminado es 900 segundos.

## Archivo de log

El worker registra simultáneamente en consola y en un archivo rotativo.

Por defecto, el log se crea junto a la base:

```text
state_pc1.sqlite
state_pc1.log
```

Puede definirse explícitamente:

```bash
python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --profile /opt/dobnow/data/chrome_profile_pc1 \
  --priorities A \
  --log-file /opt/dobnow/data/worker_pc1.log
```

Cada archivo rota al llegar a 10 MB y se conservan cinco respaldos:

```text
worker_pc1.log
worker_pc1.log.1
worker_pc1.log.2
...
```

El log informa:

- inicio y cierre del worker;
- modo standalone o CDP;
- BIN resueltos;
- metadata completada por filing;
- descargas resueltas;
- errores y tareas enviadas a `retry`;
- cooldowns;
- `Access Denied`;
- transición a `NEEDS_SESSION`;
- validación y recuperación de sesión;
- resumen final de la base.

No se escriben valores de cookies ni tokens.

## Consultar estado

No abre Chrome:

```bash
python nuevo_pipeline/worker.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --status
```

Muestra conteos de BIN, filings, documentos, estados de endpoints y descargas.

## Exportar resultados durante o después del proceso

PC 1:

```bash
python nuevo_pipeline/export_results.py \
  --db /opt/dobnow/data/state_pc1.sqlite \
  --output /opt/dobnow/data/resultado_pc1.csv
```

PC 2:

```bash
python nuevo_pipeline/export_results.py \
  --db /opt/dobnow/data/state_pc2.sqlite \
  --output /opt/dobnow/data/resultado_pc2.csv
```

La exportación es atómica: primero escribe un archivo temporal y después reemplaza el CSV final.

## Las 24 columnas contractuales

El exportador escribe exactamente este encabezado y orden:

```text
Job Filing Number
Filing Status
Filing Date
House No
Street Name
Borough
Block
LOT
Bin
Job Description
Filing Review Type
guid
filing_status
doc_description
doc_name
doc_url_original
download_url
result_status
error_body
zoning_status
doc_create_on
doc_category
doc_type_name
doc_status_label
```

Estados adicionales posibles mientras se exporta un proceso incompleto:

- `PENDING`;
- `DOWNLOAD_PENDING`;
- `DOWNLOAD_ERROR`.

Al finalizar, los estados normales son compatibles con los resultados anteriores:

- `OK`;
- `FILTERED`;
- `NO DOCUMENTS`;
- `JOB_NOT_FOUND`;
- `AKAMAI_BLOCKED`, si la exportación se hizo durante un bloqueo pendiente.

## Consolidar ambas PCs

Copia `resultado_pc1.csv` y `resultado_pc2.csv` a una misma máquina y ejecuta:

```bash
python nuevo_pipeline/merge_results.py \
  --inputs /opt/dobnow/data/resultado_pc1.csv /opt/dobnow/data/resultado_pc2.csv \
  --output /opt/dobnow/data/resultado_completo.csv
```

El consolidador:

- exige exactamente las 24 columnas;
- elimina filas exactamente duplicadas;
- informa filings presentes en más de un archivo;
- conserva una fila por documento único exportado.

El conteo esperado de filings compartidos entre las particiones es cero.

## Servicio systemd

Copia y edita el ejemplo:

```bash
sudo cp nuevo_pipeline/systemd/dobnow-worker.service.example /etc/systemd/system/dobnow-worker.service
sudo nano /etc/systemd/system/dobnow-worker.service
```

Cambia al menos:

- `User=CHANGE_ME`;
- `WorkingDirectory`;
- ruta del entorno virtual;
- ruta de la base;
- ruta del perfil Chrome;
- prioridades de ejecución.

Después:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dobnow-worker.service
sudo systemctl status dobnow-worker.service
journalctl -u dobnow-worker.service -f
```

El ejemplo espera 30 minutos antes de reiniciar después de un error o de los primeros bloqueos. Esto evita ciclos rápidos de reintento.

El servicio contiene:

```text
SuccessExitStatus=0 4 130
```

El código `4` corresponde a `NEEDS_SESSION` y systemd no lo reinicia. Después de la recuperación manual y `--check-session`, debes iniciar nuevamente el servicio.

Para detenerlo:

```bash
sudo systemctl stop dobnow-worker.service
```

## Copias de seguridad

Con el worker detenido:

```bash
cp /opt/dobnow/data/state_pc1.sqlite /opt/dobnow/backups/state_pc1_$(date +%F).sqlite
```

También pueden copiarse los archivos `-wal` y `-shm`, pero la opción más sencilla es detener el worker antes de copiar la base.

No uses `--force` como mecanismo de recuperación.

## Pruebas locales

```bash
python -m unittest discover -s nuevo_pipeline/tests -v
```

Las pruebas verifican:

- clasificación A/B/C;
- que un BIN no aparezca en las dos particiones;
- que la unión de las particiones conserve todos los BIN seleccionados;
- que el exportador produzca las 24 columnas exactas;
- que el consolidador valide y deduplique resultados.

## Flujo recomendado de puesta en marcha

1. Copiar el mismo CSV y código a ambas PCs.
2. Confirmar el mismo SHA-256 del CSV.
3. Crear `state_pc1.sqlite` con `1/2` y `state_pc2.sqlite` con `2/2`.
4. Comparar los resúmenes de preparación.
5. Ejecutar diez tareas por PC.
6. Consultar `--status`.
7. Exportar ambos CSV de prueba.
8. Validar las 24 columnas y consolidar.
9. Ejecutar prioridad A mediante systemd.
10. Analizar los nuevos resultados antes de activar B.
