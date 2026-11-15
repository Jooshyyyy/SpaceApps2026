import {Composition} from 'remotion';
import {MarsEvaDemo} from './video';

export const Root = () => (
  <Composition
    id="MarsEvaDemo"
    component={MarsEvaDemo}
    durationInFrames={900}
    fps={30}
    width={1280}
    height={720}
  />
);
