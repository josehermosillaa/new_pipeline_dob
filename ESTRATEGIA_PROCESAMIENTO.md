# Estrategia de priorización y procesamiento de documentos DOB NOW

## 1. Objetivo

El objetivo es obtener documentos asociados a las claves **ZD1, ZD1A, ZD2 y ZRD** reduciendo al mínimo posible:

- las consultas sin resultados;
- la repetición de solicitudes ya completadas;
- las interrupciones manuales;
- la pérdida de progreso después de un bloqueo o reinicio;
- las búsquedas duplicadas para filings pertenecientes al mismo BIN.

La estrategia no consiste en procesar los 240.594 filings en el orden original. Primero se procesarán los filings con mayor probabilidad de contener los documentos buscados y se utilizarán los resultados nuevos para mejorar progresivamente la selección.

## 2. Universo de datos

El CSV original contiene aproximadamente:

| Métrica | Cantidad |
|---|---:|
| Filas originales | 240.634 |
| Pares BIN–filing únicos | 240.594 |
| Job Filings únicos | 240.593 |
| BIN únicos | 89.741 |
| Promedio de filings por BIN | 2,68 |
| Máximo observado en un BIN | 405 |

Agrupar por BIN sigue siendo conveniente porque permite hacer una sola búsqueda inicial para recuperar los GUID de varios filings. Sin embargo, una vez recuperados los GUID, cada filing debe procesarse como una tarea independiente.

El flujo conceptual será:

```text
CSV original
    ↓
clasificar filings en A, B o C
    ↓
agrupar los filings seleccionados por BIN
    ↓
una búsqueda BIN → varios GUID
    ↓
crear una tarea independiente por filing
    ↓
PW1 → ZD1WD → Portal Documents
    ↓
unificar y deduplicar documentos
    ↓
downloadFromDocumentum para URLs únicas
```

## 3. Evidencia utilizada

Se analizaron dos archivos históricos de resultados:

- 12.594 filings procesados únicos;
- 12.084 filings con resultado utilizable;
- 905 filings positivos;
- 11.179 negativos sin bloqueo explícito;
- tasa positiva general de aproximadamente 7,5%.

Las señales históricas más fuertes fueron:

| Característica | Tasa positiva observada |
|---|---:|
| New Building | 96,9% |
| ALT-CO con elementos existentes | 90,4% |
| Descripción asociada a enlargement/addition | 71,1% |
| Aumento de pisos | 85,2% |
| Aumento de altura | 78,2% |
| Algún aumento de pisos, altura o unidades | 60,8% |
| Alteration CO | 29,8% |
| Alteration general | 1,5% |
| Professional Certification | 0,45% |

Estas cifras pertenecen principalmente a resultados de Manhattan y Brooklyn. Por lo tanto, inicialmente se utilizarán como una regla de priorización, no como una razón para eliminar definitivamente registros de los otros grupos o boroughs.

## 4. Definición de prioridades

### 4.1 Prioridad A: probabilidad alta

Un filing entra al grupo A cuando cumple al menos una de estas condiciones:

1. `Job Type` es `New Building`.
2. `Job Type` es `ALT-CO - New Building with Existing Elements to Remain`.
3. Aumenta el número propuesto de pisos respecto del existente.
4. Aumenta la altura propuesta respecto de la existente.
5. Aumenta el número propuesto de unidades respecto del existente.
6. `Job Description` contiene una indicación de ampliación, por ejemplo:
   - `enlargement`;
   - `addition`;
   - `add floor`;
   - `add story`;
   - `vertical`;
   - `horizontal`.

Resultados observados para A:

| Métrica | Resultado |
|---|---:|
| Filings evaluados en la muestra | 1.297 |
| Filings positivos | 772 |
| Tasa positiva | 59,5% |
| Cobertura de positivos históricos | 85,3% |
| Filings estimados en el universo completo | 47.942 |
| BIN únicos estimados | 23.138 |

### Por qué empezar por A

El grupo A concentra la mejor relación entre trabajo y resultados. En la muestra, aproximadamente seis de cada diez filings fueron positivos y el grupo capturó alrededor del 85% de todos los positivos conocidos.

Procesar A primero permite:

- obtener rápidamente la mayor cantidad de documentos;
- validar el comportamiento en todos los boroughs;
- generar nuevas etiquetas positivas y negativas;
- comprobar si las reglas históricas se mantienen;
- mejorar la priorización antes de invertir tiempo en grupos de menor rendimiento.

A un rendimiento combinado de 500 BIN diarios, A representa aproximadamente 47 días. Si cada PC procesa 500 BIN diarios y la carga se divide correctamente, serían aproximadamente 24 días. Estas cifras son referencias basadas en BIN y pueden variar según la cantidad de filings y documentos dentro de cada BIN.

## 5. Prioridad B: probabilidad media

El grupo B contiene los filings con:

```text
Job Type = Alteration CO
```

que no hayan sido incluidos previamente en A por presentar aumento de altura, pisos, unidades o palabras relacionadas con enlargement.

Resultados observados para B:

| Métrica | Resultado |
|---|---:|
| Filings evaluados en la muestra | 839 |
| Filings positivos | 76 |
| Tasa positiva | 9,1% |
| Filings estimados en el universo completo | 13.048 |

B tiene una probabilidad menor que A, pero sigue siendo considerablemente mejor que el grupo residual C.

## 6. Por qué procesar A+B como segunda etapa

Al combinar A y B se obtiene:

| Métrica | Resultado |
|---|---:|
| Filings estimados | 60.990 |
| BIN únicos estimados | 28.997 |
| Porcentaje del universo de filings | 25,3% |
| Cobertura histórica de positivos | 93,7% |
| Reducción frente al universo completo | 74,7% |

La segunda etapa no significa volver a ejecutar A. Consiste en continuar con los filings de B después de que A ya esté almacenado como completado.

El orden será:

```text
Etapa 1: procesar A
Etapa 2: procesar solamente B
Resultado acumulado: A+B
```

La razón para no comenzar directamente con A+B es que A ofrece una tasa positiva mucho mayor. Sus resultados permiten comprobar y ajustar la estrategia antes de consumir solicitudes en B.

Además, si durante A aparecen nuevas señales relevantes, éstas pueden utilizarse para reordenar B antes de procesarlo.

A un rendimiento combinado de 500 BIN diarios, A+B representa aproximadamente 58 días desde el inicio. Si cada PC procesa 500 BIN diarios, serían aproximadamente 29 días, sujeto a la distribución real de carga.

## 7. Prioridad C: probabilidad baja y grupo de control

C contiene todos los filings que no entraron en A ni B.

Resultados observados:

| Métrica | Resultado |
|---|---:|
| Filings evaluados en la muestra | 9.948 |
| Filings positivos | 57 |
| Tasa positiva | 0,57% |
| Filings estimados en el universo completo | 179.604 |

Procesar C completo al principio sería muy costoso. Sin embargo, no debe descartarse definitivamente, porque contiene algunos positivos y la muestra histórica todavía no representa todos los boroughs ni todos los tipos ZD1A, ZD2 y ZRD.

### Uso de C como muestra de control

Durante el procesamiento de A y B se reservará una pequeña proporción de capacidad para seleccionar aleatoriamente filings de C.

Distribución inicial recomendada:

```text
80% de la capacidad → A
15% de la capacidad → B
 5% de la capacidad → muestra aleatoria de C
```

La muestra de C sirve para:

- medir falsos negativos;
- descubrir reglas no contempladas;
- comprobar diferencias por borough;
- detectar patrones particulares de ZD1A, ZD2 o ZRD;
- evitar que la priorización se vuelva demasiado dependiente de los resultados históricos.

Una vez terminado A, la distribución puede cambiar a:

```text
80% de la capacidad → B
20% de la capacidad → C seleccionado o aleatorio
```

## 8. Agrupación por BIN y procesamiento por filing

La agrupación no se eliminará. Se utilizará exclusivamente para evitar búsquedas BIN repetidas.

Para el grupo A:

```text
47.942 filings
23.138 BIN únicos
```

Sin agrupar serían necesarias aproximadamente 47.942 búsquedas. Agrupando se requieren unas 23.138, lo que evita alrededor de 24.804 búsquedas redundantes.

Para A+B:

```text
60.990 filings
28.997 BIN únicos
```

La agrupación evita aproximadamente 31.993 búsquedas adicionales.

Después de obtener la respuesta del BIN, el trabajo se separará:

```text
BIN 123
  ├─ filing A → tarea independiente
  ├─ filing B → tarea independiente
  └─ filing C → tarea independiente
```

Así, un BIN con cientos de filings no se convierte en una única operación larga. Si una tarea falla, sólo queda pendiente ese filing o endpoint.

## 9. Definición de positivo, negativo y error

### Positivo

Un filing es positivo cuando cualquiera de los dos endpoints devuelve un documento cuyo nombre o tipo contiene una de las claves objetivo:

```text
ZD1
ZD1A
ZD2
ZRD
```

También deben considerarse nombres no estandarizados como:

```text
Other Documents - ZD1
revised ZD1
PAA ZD1
ZRD1
```

### Negativo confiable

Un filing sólo se considera negativo cuando:

```text
PW1 terminó correctamente
AND ZD1WD terminó correctamente
AND Portal Documents terminó correctamente
AND no apareció ninguna clave objetivo
```

### Error o resultado desconocido

No deben tratarse como negativos:

```text
AKAMAI_BLOCKED
BLOCKED_PERMANENT
FETCH_FAILED
API_ERROR
JOB_NOT_FOUND sin validar
respuesta parcial
interrupción de sesión
```

Estos casos deben quedar pendientes para reintento o revisión.

## 10. Tratamiento de los dos endpoints

Los HAR confirmaron que `ZD1WD` y `Portal Documents` pueden devolver conjuntos diferentes.

Por ello:

1. siempre se consultarán ambos endpoints para un filing seleccionado;
2. cada endpoint tendrá estado independiente;
3. se conservará la procedencia de cada documento;
4. se unirán las respuestas;
5. se deduplicará por `DocumentURL` antes de descargar;
6. se conservarán las distintas versiones y estados del documento.

Ejemplo:

```text
filing X
  PW1    = done
  ZD1WD  = done
  Portal = retry
```

Al reanudar sólo debe repetirse Portal.

## 11. Uso de las dos PCs

Las entradas no deben dividirse únicamente por cantidad de BIN. La carga estimada de un BIN será:

```text
peso del BIN = 1 + 3 × número de filings seleccionados
```

Donde:

- `1` representa la búsqueda inicial del BIN;
- `3` representa aproximadamente PW1, ZD1WD y Portal por filing;
- las descargas se medirán y distribuirán como una cola adicional.

Los BIN se ordenarán por peso de mayor a menor y se asignarán progresivamente a la PC con menor carga acumulada. Esto evita que una PC reciba los BIN con más filings y tarde mucho más que la otra.

Cada PC mantendrá su propio estado, perfil de navegador y salida. Los resultados se consolidarán posteriormente.

## 12. Métricas para decidir el avance

Durante cada etapa se medirán:

- BIN procesados;
- filings procesados;
- positivos únicos;
- documentos únicos;
- positivos por cada 100 filings;
- llamadas realizadas por positivo;
- tiempo por BIN y por filing;
- bloqueos y errores por endpoint;
- porcentaje de respuestas recuperadas desde caché;
- tasa positiva por prioridad y borough;
- tasa de positivos encontrados en la muestra de C.

### Criterio para pasar de A a B

Se comenzará a aumentar B cuando:

1. A tenga una cantidad suficiente de resultados en todos los boroughs disponibles;
2. la tasa positiva de A sea estable;
3. los errores no estén siendo etiquetados como negativos;
4. se hayan incorporado al filtro las nuevas señales encontradas;
5. el procesamiento pueda reanudarse sin repetir endpoints completados.

No es obligatorio terminar absolutamente todo A para iniciar B. B puede consumir una proporción pequeña desde el comienzo, pero A conservará la prioridad principal.

## 13. Resultado esperado

La estrategia busca pasar de:

```text
procesar 240.594 filings sin orden
```

a:

```text
procesar primero 47.942 filings de alta probabilidad
    ↓
capturar aproximadamente 85% de los positivos históricos
    ↓
procesar 13.048 filings adicionales de prioridad B
    ↓
alcanzar aproximadamente 94% de cobertura histórica
    ↓
usar muestras de C para medir y corregir lo que falta
```

Las cifras de cobertura son estimaciones basadas en los resultados históricos disponibles. Deben actualizarse a medida que se incorporen datos de Queens, Bronx y Staten Island y más ejemplos de ZD1A, ZD2 y ZRD.

## 14. Decisión estratégica resumida

1. **Mantener la agrupación por BIN** para reducir búsquedas repetidas.
2. **Filtrar los filings antes de agruparlos** según prioridades A, B y C.
3. **Procesar A primero** por su alta tasa positiva y cobertura.
4. **Continuar con B** para elevar la cobertura acumulada.
5. **Muestrear C continuamente** para controlar falsos negativos.
6. **Separar el trabajo por filing después de obtener los GUID**.
7. **Guardar el estado de cada endpoint**, evitando repetir trabajo terminado.
8. **Deduplicar documentos por URL** antes de solicitar descargas.
9. **Dividir las dos PCs por carga estimada**, no sólo por número de BIN.
10. **Recalcular las prioridades con cada nuevo lote de resultados**.

Esta estrategia prioriza la obtención temprana de documentos y la reducción de trabajo inútil, sin asumir que el grupo de baja probabilidad carece completamente de resultados.
