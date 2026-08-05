# 09. K8s 실기동 리허설 — 노트북에서 밟는다

> `docs/07 §3.2`에서 "클러스터가 있는 환경이 필요하다"로 미뤄 둔 항목이다.
> **GPU가 필요 없다는 것**을 확인하고 나니 Apple Silicon 노트북으로 옮길 수 있었다.

## 1. 무엇을 확인하는 리허설인가

CI는 이미 **렌더링과 쿠버네티스 스키마 적합성**까지 본다(오버레이 5종, 211개
리소스, `helm lint` + `kubeconform`). 남은 것은 그 다음이다:

| 확인할 것 | 왜 CI로는 안 되나 |
|---|---|
| 실제 스케줄링 | 리소스 요청·노드 용량은 클러스터가 있어야 판정된다 |
| PVC 바인딩 | StorageClass가 없으면 파드가 `Pending`으로 멈춘다 |
| 프로브 통과 | `readinessProbe` 경로·포트가 맞는지는 기동해야 안다 |
| 단일 라이터 강제 | `replicas: 1` + `Recreate`가 실제로 지켜지는지 |
| 기동 순서 | 의존 블록이 아직 없을 때 죽지 않고 기다리는지 |

**GPU는 여기 없다.** 모델 품질과 성능 프로파일은 별개 작업이며, 그쪽은 실서버가
필요하다. 섞으면 "클러스터 리허설은 GPU 서버가 있어야 한다"는 잘못된 결론이 난다.

## 2. 왜 arm64 이미지가 필요한가

Apple Silicon은 arm64다. `linux/amd64` 이미지만 있으면 QEMU 에뮬레이션으로 돌고,
21개 블록이 전부 느려진다. 리허설이 "느려서 못 하겠다"가 되면 안 하게 된다.

빌드 정책:

| 언제 | 아키텍처 |
|---|---|
| PR | `linux/amd64` — 검사 시간을 두 배로 만들지 않는다 |
| `main` push | `linux/amd64,linux/arm64` |
| **납품 번들** | **`amd64` 고정** — 고객사 서버는 x86이고, 둘을 담으면 번들이 두 배가 된다 |

리허설용 번들은 명시적으로 요청한다:

```bash
deploy/airgap/build_bundle.sh --block UI-MEET --arch arm64 --out dist/
```

### 검증된 것

arm64 빌드는 **실제로 돌려 확인했다**(QEMU 에뮬레이션, `docker buildx`):

| 블록 | 무엇을 대표하나 | 결과 |
|---|---|---|
| `core-bus` | 최소 의존 + 기동 | ✓ 빌드 · **컨테이너 기동 · `/readyz` 응답**(aarch64, Python 3.12.13) |
| `core-sec` | `cryptography` (Rust 확장) | ✓ 빌드 |
| `stt-core` | `numpy` | ✓ 빌드 |
| `rag-srch` | `numpy` + `vai-retrieval` | ✓ 빌드 |
| `core-adm` | `pyjwt` · `httpx` | ✓ 빌드 |

의존성 해석도 별도로 확인했다 — 필수(`cryptography`·`numpy`·`pydantic-core`·
`pyyaml`)와 선택(`faster-whisper`·`kiwipiepy`·`qdrant-client`·
`sentence-transformers`) 모두 aarch64 휠이 있다.

번들에 `ARCH` 파일이 함께 들어가고, `install.sh`가 서버 아키텍처와 대조한다.
어긋난 번들을 적재하면 컨테이너가 `exec format error`로 죽는데, 그 문구만으로는
원인을 짚기 어렵고 그때는 이미 이미지를 다 푼 뒤다.

## 3. 절차 (16GB Apple Silicon 기준)

### 3.1 메모리부터 계산한다

16GB에서 OS·앱이 4~5GB를 쓴다. 컨테이너에 쓸 수 있는 것은 **10~11GB**다.

| 패키지 | 블록 | 대략 |
|---|---|---|
| `meeting` | 15 | ~3.5GB + Redis + Qdrant |
| `aicc` | 21 | ~5GB + 인프라 |

**`meeting`부터 한다.** 전 블록을 한 번에 올리면 메모리 압박으로 파드가 재시작되고,
그러면 재시작이 설정 문제인지 메모리 문제인지 구분되지 않는다.

### 3.2 클러스터

로컬에서 쿠버네티스를 띄우는 도구는 아무거나 된다 — 우리 차트가 배포판에
묶여 있지 않기 때문이다. `k3d`(가볍다)나 `kind`(업스트림 k8s에 가장 가깝다)를 쓴다.

```bash
brew install k3d kubectl helm
k3d cluster create visionai --agents 1 \
  --k3s-arg "--disable=traefik@server:0"      # 인그레스는 이 리허설의 대상이 아니다

# 또는 업스트림 K8s 에 더 가깝게:
#   brew install kind && kind create cluster --name visionai
```

**주의: 기본 StorageClass가 있어야 한다.** 없으면 PVC가 `Pending`으로 멈추고
파드가 영원히 안 뜬다. `k3d`는 `local-path`가 기본으로 붙고, `kind`도 기본
provisioner가 있다. 고객사 클러스터에서는 그쪽 StorageClass 이름을
`--set ...persistence.storageClass=` 로 넘긴다.

### 3.3 이미지 적재

```bash
for b in $(uv run blockctl list --root . | awk '{print tolower($1)}'); do
  docker build --platform linux/arm64 -f deploy/docker/Dockerfile \
    --build-arg "BLOCK=$b" -t "visionai/$b:0.1.0" .
  k3d image import "visionai/$b:0.1.0" -c visionai
done
```

`k3d image import`가 필요한 이유는 폐쇄망과 같다 — 레지스트리가 없다.

### 3.4 배포

```bash
helm install visionai deploy/charts/visionai -f deploy/charts/visionai/values-meeting.yaml \
  --set image.tag=0.1.0 --set image.pullPolicy=Never
```

`pullPolicy=Never`가 핵심이다. `IfNotPresent`여도 태그가 어긋나면 레지스트리를
찾으러 나가고, 폐쇄망에서는 그대로 기동 실패가 된다.

### 3.5 무엇을 보는가

```bash
kubectl get pods -w                    # Pending 이 남으면 스케줄링·PVC 문제
kubectl get pvc                        # Bound 가 아니면 StorageClass 문제
kubectl describe pod <이름>            # 프로브 실패는 Events 에 나온다
kubectl logs -l app.kubernetes.io/name=visionai --tail=50
```

기동이 끝나면 **운영 콘솔의 블록 시험 탭**(`docs/06`)으로 입출력까지 확인한다.
`/readyz`가 초록인 것과 블록이 실제로 일하는 것은 다르다 — 그 차이가 감사에서
나온 결함 12건의 공통점이었다.

## 4. 리허설로 알 수 없는 것

정직하게 적어 둔다. 여기서 통과해도 남는 것들이다:

- **성능** — 노트북 1노드는 동시 채널 프로파일의 근거가 되지 않는다
- **모델 품질** — fake 어댑터로 돌므로 STT/TTS 정확도는 여기서 안 나온다
- **다중 노드 동작** — 단일 라이터 블록이 노드 간에 어떻게 움직이는지
- **실 고객사 스토리지** — StorageClass 구현마다 PVC 동작이 다르다

이 넷은 실서버가 필요하고, `docs/07 §3.2`에 그대로 남는다.
