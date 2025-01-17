import { useAppDispatch, useAppSelector } from 'app/store/storeHooks';
import { isInputElement } from 'common/util/isInputElement';
import {
  $canvasStage,
  $tool,
  $toolStash,
  resetCanvasInteractionState,
  resetToolInteractionState,
} from 'features/canvas/store/canvasNanostore';
import { isStagingSelector } from 'features/canvas/store/canvasSelectors';
import {
  clearMask,
  setIsMaskEnabled,
  setShouldShowBoundingBox,
  setShouldSnapToGrid,
} from 'features/canvas/store/canvasSlice';
import { isElChildOfCanvasTab } from 'features/canvas/util/isElChildOfCanvasTab';
import { activeTabNameSelector } from 'features/ui/store/uiSelectors';
import { useCallback, useEffect } from 'react';
import { useHotkeys } from 'react-hotkeys-hook';

const useInpaintingCanvasHotkeys = () => {
  const dispatch = useAppDispatch();
  const activeTabName = useAppSelector(activeTabNameSelector);
  const shouldShowBoundingBox = useAppSelector(
    (s) => s.canvas.shouldShowBoundingBox
  );
  const isStaging = useAppSelector(isStagingSelector);
  const isMaskEnabled = useAppSelector((s) => s.canvas.isMaskEnabled);
  const shouldSnapToGrid = useAppSelector((s) => s.canvas.shouldSnapToGrid);

  // Beta Keys
  const handleClearMask = useCallback(() => dispatch(clearMask()), [dispatch]);

  useHotkeys(
    ['shift+c'],
    () => {
      handleClearMask();
    },
    {
      enabled: () => !isStaging,
      preventDefault: true,
    },
    []
  );

  const handleToggleEnableMask = () =>
    dispatch(setIsMaskEnabled(!isMaskEnabled));

  useHotkeys(
    ['h'],
    () => {
      handleToggleEnableMask();
    },
    {
      enabled: () => !isStaging,
      preventDefault: true,
    },
    [isMaskEnabled]
  );

  useHotkeys(
    ['n'],
    () => {
      dispatch(setShouldSnapToGrid(!shouldSnapToGrid));
    },
    {
      enabled: true,
      preventDefault: true,
    },
    [shouldSnapToGrid]
  );
  //

  useHotkeys(
    'esc',
    () => {
      resetCanvasInteractionState();
    },
    {
      enabled: () => true,
      preventDefault: true,
    }
  );

  useHotkeys(
    'shift+h',
    () => {
      dispatch(setShouldShowBoundingBox(!shouldShowBoundingBox));
    },
    {
      enabled: () => !isStaging,
      preventDefault: true,
    },
    [activeTabName, shouldShowBoundingBox]
  );

  const onKeyDown = useCallback((e: KeyboardEvent) => {
    if (
      e.repeat ||
      e.key !== ' ' ||
      isInputElement(e.target as HTMLElement) ||
      !isElChildOfCanvasTab(e.target as HTMLElement)
    ) {
      return;
    }
    if ($toolStash.get() || $tool.get() === 'move') {
      return;
    }
    $canvasStage.get()?.container().focus();
    $toolStash.set($tool.get());
    $tool.set('move');
    resetToolInteractionState();
  }, []);
  const onKeyUp = useCallback((e: KeyboardEvent) => {
    if (
      e.repeat ||
      e.key !== ' ' ||
      isInputElement(e.target as HTMLElement) ||
      !isElChildOfCanvasTab(e.target as HTMLElement)
    ) {
      return;
    }
    if (!$toolStash.get() || $tool.get() !== 'move') {
      return;
    }
    $canvasStage.get()?.container().focus();
    $tool.set($toolStash.get() ?? 'move');
    $toolStash.set(null);
  }, []);

  useEffect(() => {
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [onKeyDown, onKeyUp]);
};

export default useInpaintingCanvasHotkeys;
