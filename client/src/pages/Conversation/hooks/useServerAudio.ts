import { useCallback, useEffect, useRef, useState } from "react";
import { useSocketContext } from "../SocketContext";
import { decodeMessage } from "../../../protocol/encoder";
import { useMediaContext } from "../MediaContext";
import { createDecoderWorker, initDecoder, getPrewarmedWorker } from "../../../decoder/decoderWorker";

export type AudioStats = {
  playedAudioDuration: number;
  missedAudioDuration: number;
  totalAudioMessages: number;
  delay: number;
  minPlaybackDelay: number;
  maxPlaybackDelay: number;
};

type useServerAudioArgs = {
  setGetAudioStats?: (getAudioStats: () => AudioStats) => void;
};

type WorkletStats = {
  totalAudioPlayed: number;
  actualAudioPlayed: number;
  delay: number;
  minDelay: number;
  maxDelay: number;
};

export const useServerAudio = ({setGetAudioStats}: useServerAudioArgs) => {
  const { socket, socketStatus } = useSocketContext();
  const {startRecording, stopRecording, audioContext, worklet, micDuration, actualAudioPlayed } =
    useMediaContext();
  const analyser = useRef(audioContext.current.createAnalyser());
  worklet.current.connect(analyser.current);
  const startTime = useRef<number | null>(null);
  const decoderWorker = useRef<Worker | null>(null);
  const [decoderReady, setDecoderReady] = useState(false);
  const [hasCriticalDelay, setHasCriticalDelay] = useState(false);
  const totalAudioMessages = useRef(0);
  const receivedDuration = useRef(0);
  const workletStats = useRef<WorkletStats>({
    totalAudioPlayed: 0,
    actualAudioPlayed: 0,
    delay: 0,
    minDelay: 0,
    maxDelay: 0,});

  const onDecode = useCallback(
    async (data: Float32Array) => {
      receivedDuration.current += data.length / audioContext.current.sampleRate;
      worklet.current.port.postMessage({frame: data, type: "audio", micDuration: micDuration.current});
    },
    [],
  );

  const onWorkletMessage = useCallback(
    (event: MessageEvent<WorkletStats>) => {
      workletStats.current = event.data;
      actualAudioPlayed.current = workletStats.current.actualAudioPlayed;
    },
    [],
  );
  worklet.current.port.onmessage = onWorkletMessage;

  const getAudioStats = useCallback(() => {
    return {
      playedAudioDuration: workletStats.current.actualAudioPlayed,
      delay: workletStats.current.delay,
      minPlaybackDelay: workletStats.current.minDelay,
      maxPlaybackDelay: workletStats.current.maxDelay,
      missedAudioDuration: workletStats.current.totalAudioPlayed - workletStats.current.actualAudioPlayed,
      totalAudioMessages: totalAudioMessages.current,
    };
  }, []);

  const onWorkerMessage = useCallback(
    (e: MessageEvent<any>) => {
      if (!e.data) {
        return;
      }
      onDecode(e.data[0]);
    },
    [onDecode],
  );

  let midx = 0;
  const audioBytesReceived = useRef(0);
  const audioFramesReceived = useRef(0);
  const lastClientLogTime = useRef(Date.now());
  
  const decodeAudio = useCallback((data: Uint8Array) => {
    if (!decoderWorker.current) {
      console.warn("Decoder worker not ready, dropping audio packet");
      return;
    }
    
    // Log every message with size and OggS check
    const hasOggS = data.length >= 4 && 
      data[0] === 0x4F && data[1] === 0x67 && data[2] === 0x67 && data[3] === 0x53;
    console.log(Date.now() % 1000, "Got NETWORK message", 
      "size:", data.length, 
      "hasOggS:", hasOggS);
    
    audioBytesReceived.current += data.length;
    audioFramesReceived.current += 1;
    
    // Log summary every 1 second
    const now = Date.now();
    if (now - lastClientLogTime.current >= 1000) {
      const avgFrameSize = audioBytesReceived.current / audioFramesReceived.current;
      console.log("CLIENT_AUDIO: total_bytes=", audioBytesReceived.current,
        "frames=", audioFramesReceived.current,
        "avg_frame_size=", avgFrameSize.toFixed(0));
      audioBytesReceived.current = 0;
      audioFramesReceived.current = 0;
      lastClientLogTime.current = now;
    }
    
    decoderWorker.current.postMessage(
      {
        command: "decode",
        pages: data,
      },
      [data.buffer],
    );
  }, []);

  const onSocketMessage = useCallback(
    (e: MessageEvent) => {
      const dataArray = new Uint8Array(e.data);
      const message = decodeMessage(dataArray);
      if (message.type === "audio") {
        decodeAudio(message.data);
        //For stats purposes for now
        totalAudioMessages.current++;
      }
    },
    [decodeAudio],
  );

  useEffect(() => {
    const currentSocket = socket;
    if (!currentSocket || socketStatus !== "connected" || !decoderReady) {
      return;
    }
    worklet.current.port.postMessage({type: "reset"});
    console.log(Date.now() % 1000, "Should start in a bit - decoder ready:", decoderReady);
    startRecording();
    currentSocket.addEventListener("message", onSocketMessage);
    totalAudioMessages.current = 0;
    audioBytesReceived.current = 0;
    audioFramesReceived.current = 0;
    lastClientLogTime.current = Date.now();
    return () => {
      console.log("Stop recording called in unknown function.")
      stopRecording();
      startTime.current = null;
      currentSocket.removeEventListener("message", onSocketMessage);
    };
  }, [socket, socketStatus, decoderReady]);

  useEffect(() => {
    if (setGetAudioStats) {
      setGetAudioStats(getAudioStats);
    }
  }, [setGetAudioStats, getAudioStats]);

  // Use prewarmed worker if available, otherwise create fresh one
  useEffect(() => {
    let mounted = true;
    let workerInstance: Worker | null = null;
    
    const setupWorker = async () => {
      // Try to get prewarmed worker (was started when user clicked Connect on homepage)
      const prewarmed = await getPrewarmedWorker();
      
      if (!mounted) return;
      
      if (prewarmed) {
        console.log("Using prewarmed decoder worker");
        workerInstance = prewarmed;
      } else {
        console.log("No prewarmed worker available, creating fresh one");
        workerInstance = createDecoderWorker();
      }
      
      decoderWorker.current = workerInstance;
      
      // Set up message handler
      workerInstance.onmessage = onWorkerMessage;
      
      if (prewarmed) {
        // Prewarmed worker is already initialized
        console.log("Prewarmed decoder worker ready, setting decoderReady=true");
        setDecoderReady(true);
      } else {
        // Initialize fresh worker and wait for it to be ready
        await initDecoder(workerInstance, audioContext.current.sampleRate);
        if (mounted) {
          console.log("Fresh decoder worker ready, setting decoderReady=true");
          setDecoderReady(true);
        }
      }
    };
    
    setupWorker();

    return () => {
      mounted = false;
      console.log("Terminating decoder worker");
      if (workerInstance) {
        workerInstance.terminate();
      }
      decoderWorker.current = null;
      setDecoderReady(false);
    };
  }, [onWorkerMessage]);

  return {
    decodeAudio,
    analyser,
    getAudioStats,
    hasCriticalDelay,
    setHasCriticalDelay,
  };
};
