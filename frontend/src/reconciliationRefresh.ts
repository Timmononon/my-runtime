export function createReconciliationRefresh({
  refresh,
  isActive,
  keepPolling,
  onError,
}: {
  refresh: () => Promise<void>;
  isActive: () => boolean;
  keepPolling: boolean;
  onError: (error: unknown) => void;
}) {
  let stopped = false,
    inFlight = false,
    pending = false,
    delay = 1800;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clear = () => {
    clearTimeout(timer);
    timer = undefined;
  };
  const check = () => {
    clear();
    if (stopped || !isActive()) return;
    if (inFlight) {
      pending = true;
      return;
    }
    inFlight = true;
    void Promise.resolve()
      .then(refresh)
      .catch((error) => {
        if (!stopped) onError(error);
      })
      .finally(() => {
        inFlight = false;
        if (stopped || !isActive()) return;
        if (pending) {
          pending = false;
          check();
        } else if (keepPolling) {
          // One read chain at a time; long-lived barriers back off to 10s.
          // A focus/visibility wake starts an immediate check again.
          timer = setTimeout(check, delay);
          delay = Math.min(delay * 2, 10000);
        }
      });
  };
  return {
    wake() {
      delay = 1800;
      check();
    },
    pause() {
      clear();
      pending = false;
    },
    dispose() {
      stopped = true;
      clear();
      pending = false;
    },
  };
}
