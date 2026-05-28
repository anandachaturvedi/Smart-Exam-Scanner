import React, { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import './App.css';

const initialMetrics = {
  scoreText: '-',
  scoreDetail: '',
  coverageText: '-',
  accuracyText: '-',
  scoreClass: '',
  coverageClass: '',
  accuracyClass: '',
};

const sidebarItems = ['New evaluation'];
const toolItems = ['Question bank', 'History', 'Chat'];
const questionBankSections = [
  {
    key: 'notes',
    title: 'Subject-wise notes',
    items: [],
  },
  {
    key: 'pyqs',
    title: 'PYQs',
    items: [],
  },
  {
    key: 'books',
    title: 'Books',
    items: [],
  },
  {
    key: 'solved-books',
    title: 'Solved books',
    items: [],
  },
];
function getMetricClass(value) {
  if (value >= 80) return 'good';
  if (value >= 55) return 'warn';
  return 'bad';
}

function splitFeedback(feedback) {
  return feedback
    .split(/\n+/)
    .map((item) => item.replace(/^[-*]\s*/, '').trim())
    .filter(Boolean)
    .map((item, index) => {
      const lowered = item.toLowerCase();
      let type = 'Suggestion';
      let cls = '';

      if (lowered.includes('strength') || lowered.includes('good') || lowered.includes('well done')) {
        type = 'Strength';
        cls = 'good';
      } else if (
        lowered.includes('missing') ||
        lowered.includes('improve') ||
        lowered.includes('lack') ||
        lowered.includes('incorrect')
      ) {
        type = 'Missing point';
        cls = 'warn';
      }

      return {
        id: `${index}-${item.slice(0, 16)}`,
        type,
        cls,
        text: item,
      };
    });
}

function extractFeedbackQuestionLabel(text, index) {
  const qMatch = text.match(/\bQ(?:uestion)?\s*(\d+)\b/i);
  if (qMatch) {
    return `Q${qMatch[1]}`;
  }

  const sectionMatch = text.match(/\bPart\s+([A-Z0-9]+)\b/i);
  if (sectionMatch) {
    return `Part ${sectionMatch[1].toUpperCase()}`;
  }

  return '';
}

function cleanFeedbackCardText(text, type) {
  return cleanMarkdownForDisplay(text)
    .replace(new RegExp(`^${type}:\\s*`, 'i'), '')
    .replace(/^Accuracy issue:\s*/i, '')
    .replace(/^(?:In|For)\s+Q(?:uestion)?\s*\d+\s*,?\s*/i, '')
    .replace(/^Q(?:uestion)?\s*\d+\s*,?\s*/i, '')
    .trim();
}

function buildFallbackFeedback(userText, topperText, questionText) {
  const promptTopic = questionText ? ` for "${questionText}"` : '';
  const userLength = userText.trim().split(/\s+/).filter(Boolean).length;
  const topperLength = topperText.trim().split(/\s+/).filter(Boolean).length;

  return [
    {
      id: 'fallback-strength',
      type: 'Strength',
      cls: 'good',
      text: `Your answer${promptTopic} has been captured successfully and is ready for review.`,
    },
    {
      id: 'fallback-coverage',
      type: 'Missing point',
      cls: 'warn',
      text:
        userLength < topperLength
          ? 'Your answer is shorter than the reference answer, so you may be missing some supporting points or examples.'
          : 'Your answer length is comparable to the reference answer, but check whether each key point is clearly stated.',
    },
    {
      id: 'fallback-suggestion',
      type: 'Suggestion',
      cls: '',
      text: 'Use short point-wise sentences and include one precise example or definition to improve marks.',
    },
  ];
}

const genericReferenceTitles = new Set(['current affairs', 'daily tests', 'mock interview', 'portals']);
const examSearchHistoryKey = 'smart-exam-scanner.exam-search-history';
const subjectSearchHistoryKey = 'smart-exam-scanner.subject-search-history';
const defaultExamSearchHistory = [
  'cbse class 10 2024',
  'cbse class 10',
  'upsc',
  'cbse class 12',
  'up board class 12',
  'maharashtra board class 10',
];
const defaultSubjectSearchHistory = [
  'Maths standard',
  'maths',
  'Physics',
  'English Language & Literature',
  'current affairs',
  'marathi',
];
const initialChatMessages = [
  {
    id: 'welcome',
    role: 'assistant',
    content:
      'Hi, I’m your EvalAI chat partner. Ask me anything, ask for interview practice, or ask me to review your current sheet and comparison.',
  },
];

function loadSearchHistory(key, defaults) {
  if (typeof window === 'undefined') return defaults;

  try {
    const saved = JSON.parse(window.localStorage.getItem(key) || '[]');
    const merged = [...saved, ...defaults];
    return [...new Map(merged.filter(Boolean).map((item) => [item.trim().toLowerCase(), item.trim()])).values()].slice(0, 12);
  } catch {
    return defaults;
  }
}

function saveSearchHistory(key, items) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(key, JSON.stringify(items.slice(0, 12)));
}

function cleanMarkdownForDisplay(value) {
  return (value || '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1$2')
    .trim();
}

function formatHistoryDate(value) {
  return value ? new Date(value).toLocaleDateString() : '';
}

function buildComparisonContextSummary({ examBoard, subject, metrics, feedbackBlocks }) {
  const parts = [];
  const label = [examBoard, subject].filter(Boolean).join(' | ');
  if (label) {
    parts.push(`Exam context: ${label}`);
  }

  const metricBits = [
    metrics.scoreText ? `Score ${metrics.scoreText}` : '',
    metrics.coverageText ? `Coverage ${metrics.coverageText}` : '',
    metrics.accuracyText ? `Accuracy ${metrics.accuracyText}` : '',
  ].filter(Boolean);

  if (metricBits.length) {
    parts.push(metricBits.join(' | '));
  }

  if (feedbackBlocks?.length) {
    parts.push(
      feedbackBlocks
        .slice(0, 6)
        .map((item) => `${item.type}: ${item.text}`)
        .join('\n')
    );
  }

  return parts.join('\n\n').trim();
}

function getMetricLabel(metricKey) {
  const labels = {
    coverage: 'Coverage',
    accuracy: 'Accuracy',
    terminology: 'Terminology',
    depth: 'Depth',
  };
  return labels[metricKey] || metricKey;
}

function buildComparisonInsights(resultPayload, feedbackBlocks, topperText) {
  const questionScores = Array.isArray(resultPayload?.question_scores) ? resultPayload.question_scores : [];
  const weights = resultPayload?.weights || null;
  const metricsPayload = resultPayload?.metrics || {};

  let highestYield = {
    title: 'Highest mark opportunity',
    text: 'Run a comparison to see which question or scoring area carries the most marks.',
  };

  if (questionScores.length > 0) {
    const topMarks = Math.max(...questionScores.map((item) => Number(item.max_marks) || 0));
    const heavyQuestions = questionScores.filter((item) => Number(item.max_marks) === topMarks);
    highestYield = {
      title: `Most marks sit in ${heavyQuestions.map((item) => `Q${item.question_number}`).join(', ')}`,
      text: `${heavyQuestions.map((item) => `Q${item.question_number}`).join(', ')} carry ${topMarks} marks each. Give these the clearest structure first because they move the total the most.`,
    };
  } else if (weights) {
    const heaviestMetric = Object.entries(weights).sort((left, right) => (right[1] || 0) - (left[1] || 0))[0];
    if (heaviestMetric) {
      highestYield = {
        title: `${getMetricLabel(heaviestMetric[0])} drives the most marks`,
        text: `${getMetricLabel(heaviestMetric[0])} contributes about ${Math.round((heaviestMetric[1] || 0) * 100)}% of the score here, so improving that part will move your marks fastest.`,
      };
    }
  } else {
    const rankedMetrics = ['coverage', 'accuracy', 'terminology', 'depth']
      .map((key) => [key, Number(metricsPayload[key]) || 0])
      .sort((left, right) => left[1] - right[1]);
    if (rankedMetrics[0]) {
      highestYield = {
        title: `${getMetricLabel(rankedMetrics[0][0])} is your biggest mark lever`,
        text: `Your ${getMetricLabel(rankedMetrics[0][0]).toLowerCase()} is currently the softest area, so improving it should lift the score the quickest.`,
      };
    }
  }

  let struggleAreas = [];
  if (questionScores.length > 0) {
    struggleAreas = [...questionScores]
      .filter((item) => {
        const earnedMarks = Number(item.earned_marks) || 0;
        const maxMarks = Number(item.max_marks) || 0;
        if (maxMarks <= 0) return false;
        return (earnedMarks / maxMarks) * 100 < 50;
      })
      .sort((left, right) => {
        const leftMax = Number(left.max_marks) || 0;
        const rightMax = Number(right.max_marks) || 0;
        const leftPercentage = leftMax > 0 ? ((Number(left.earned_marks) || 0) / leftMax) * 100 : 0;
        const rightPercentage = rightMax > 0 ? ((Number(right.earned_marks) || 0) / rightMax) * 100 : 0;
        return leftPercentage - rightPercentage;
      })
      .map((item) => `Q${item.question_number}: ${item.earned_marks}/${item.max_marks} marks (${item.percentage}%)`);
  }

  if (!struggleAreas.length) {
    struggleAreas = (feedbackBlocks || [])
      .filter((item) => item.cls === 'warn' || item.type === 'Accuracy issue')
      .slice(0, 3)
      .map((item) => item.text);
  }

  if (!struggleAreas.length) {
    struggleAreas = ['No questions are currently below 50% of their total marks.'];
  }

  return {
    highestYield,
    struggleAreas,
  };
}

function buildMetricsFromHistory(resultPayload, userText, topperText, questionText = '') {
  const feedbackText = resultPayload?.feedback || '';
  const parsedBlocks = splitFeedback(feedbackText);
  const fallbackBlocks = buildFallbackFeedback(userText, topperText, questionText);
  const finalBlocks = parsedBlocks.length ? parsedBlocks : fallbackBlocks;
  const backendMetrics = resultPayload?.metrics || {};
  const coverage = Number.isFinite(backendMetrics.coverage)
    ? Math.round(backendMetrics.coverage)
    : 0;
  const accuracy = Number.isFinite(backendMetrics.accuracy)
    ? Math.round(backendMetrics.accuracy)
    : 0;
  const overall = Number.isFinite(backendMetrics.overall)
    ? Math.round(backendMetrics.overall)
    : Math.round((coverage + accuracy) / 2);
  const scoreValue = Math.max(0, Math.min(100, overall));

  return {
    metrics: {
      scoreText: `${scoreValue}/100`,
      scoreDetail: '',
      coverageText: `${coverage}%`,
      accuracyText: `${accuracy}%`,
      scoreClass: getMetricClass(scoreValue),
      coverageClass: getMetricClass(coverage),
      accuracyClass: getMetricClass(accuracy),
    },
    feedbackBlocks: finalBlocks,
  };
}

function normalizeSearchText(value) {
  return (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function isCbseResultForDifferentExam(result, requestedExam) {
  const exam = normalizeSearchText(requestedExam);
  const source = normalizeSearchText(result.source);
  const section = normalizeSearchText(result.section);
  return !exam.includes('cbse') && (source === 'cbse' || section.includes('cbse'));
}

function rankReferenceResults(results, requestedSubject, requestedExam) {
  const subjectTokens = normalizeSearchText(requestedSubject).split(/\s+/).filter(Boolean);

  return results
    .filter((result) => !isCbseResultForDifferentExam(result, requestedExam))
    .map((result, index) => {
      const title = normalizeSearchText(result.subject_name);
      const url = normalizeSearchText(result.download_url);
      const titleTokens = new Set(title.split(/\s+/).filter(Boolean));
      const hasSubjectInTitle = subjectTokens.some((token) => titleTokens.has(token));
      const hasSubjectAnywhere = subjectTokens.some((token) => title.includes(token) || url.includes(token));
      const isGeneric = genericReferenceTitles.has(title);

      let rank = 0;
      if (hasSubjectInTitle) rank += 100;
      if (hasSubjectAnywhere) rank += 25;
      if (title === normalizeSearchText(requestedSubject)) rank += 50;
      if ((result.file_type || '').toUpperCase() === 'PDF') rank += 10;
      if (isGeneric && !hasSubjectInTitle) rank -= 200;

      return { ...result, _rank: rank, _originalIndex: index, _hiddenGeneric: isGeneric && !hasSubjectInTitle };
    })
    .filter((result) => !result._hiddenGeneric)
    .sort((left, right) => {
      if (right._rank !== left._rank) return right._rank - left._rank;
      return left._originalIndex - right._originalIndex;
    });
}

function App() {
  const [currentView, setCurrentView] = useState('evaluation');
  const [currentStep, setCurrentStep] = useState(1);
  const [file, setFile] = useState(null);
  const [imagePreview, setImagePreview] = useState('');
  const [referenceFile, setReferenceFile] = useState(null);
  const [referencePreview, setReferencePreview] = useState('');
  const [userAnswer, setUserAnswer] = useState('');
  const [topperAnswer, setTopperAnswer] = useState('');
  const [examBoard, setExamBoard] = useState('');
  const [subject, setSubject] = useState('');
  const [questionText, setQuestionText] = useState('');
  const [maxMarks, setMaxMarks] = useState('');
  const [ocrLoading, setOcrLoading] = useState(false);
  const [ocrProgress, setOcrProgress] = useState(0);
  const [ocrError, setOcrError] = useState('');
  const [referenceLoading, setReferenceLoading] = useState(false);
  const [referenceProgress, setReferenceProgress] = useState(0);
  const [referenceUploadError, setReferenceUploadError] = useState('');
  const [referenceLookupError, setReferenceLookupError] = useState('');
  const [referenceSource, setReferenceSource] = useState('');
  const [referenceMode, setReferenceMode] = useState('upload');
  const [referenceResults, setReferenceResults] = useState([]);
  const [downloadingReferenceId, setDownloadingReferenceId] = useState('');
  const [referenceDownloadMessage, setReferenceDownloadMessage] = useState('');
  const [fetchingTopper, setFetchingTopper] = useState(false);
  const [examSearchHistory, setExamSearchHistory] = useState(() => loadSearchHistory(examSearchHistoryKey, defaultExamSearchHistory));
  const [subjectSearchHistory, setSubjectSearchHistory] = useState(() =>
    loadSearchHistory(subjectSearchHistoryKey, defaultSubjectSearchHistory)
  );
  const [examSearchFocused, setExamSearchFocused] = useState(false);
  const [subjectSearchFocused, setSubjectSearchFocused] = useState(false);
  const [examSearchActiveIndex, setExamSearchActiveIndex] = useState(0);
  const [subjectSearchActiveIndex, setSubjectSearchActiveIndex] = useState(0);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState('');
  const [metrics, setMetrics] = useState(initialMetrics);
  const [feedbackBlocks, setFeedbackBlocks] = useState([]);
  const [comparisonResult, setComparisonResult] = useState(null);
  const [evaluationDone, setEvaluationDone] = useState(false);
  const [askQuestionText, setAskQuestionText] = useState('');
  const [askAnswerText, setAskAnswerText] = useState('');
  const [askQuestionLoading, setAskQuestionLoading] = useState(false);
  const [askQuestionUploadLoading, setAskQuestionUploadLoading] = useState(false);
  const [askAnswerUploadLoading, setAskAnswerUploadLoading] = useState(false);
  const [askQuestionError, setAskQuestionError] = useState('');
  const [askQuestionAnswer, setAskQuestionAnswer] = useState('');
  const [askQuestionImages, setAskQuestionImages] = useState([]);
  const [questionBankQuery, setQuestionBankQuery] = useState('');
  const [questionBankResults, setQuestionBankResults] = useState(questionBankSections);
  const [questionBankLoading, setQuestionBankLoading] = useState(false);
  const [questionBankError, setQuestionBankError] = useState('');
  const [historyData, setHistoryData] = useState({ evaluations: [], ask_messages: [], chat_sessions: [] });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [historyOpeningId, setHistoryOpeningId] = useState('');
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMinimized, setChatMinimized] = useState(false);
  const [chatMode, setChatMode] = useState('auto');
  const [autoChatMessages, setAutoChatMessages] = useState(initialChatMessages);
  const [interviewerMessages, setInterviewerMessages] = useState([]);
  const [chatInput, setChatInput] = useState('');
  const [chatLoading, setChatLoading] = useState(false);
  const [chatError, setChatError] = useState('');
  const [activeAutoChatSessionId, setActiveAutoChatSessionId] = useState(null);
  const [interviewPosition, setInterviewPosition] = useState('');
  const [interviewTopic, setInterviewTopic] = useState('');
  const [interviewFocus, setInterviewFocus] = useState('');
  const [interviewStarted, setInterviewStarted] = useState(false);
  const [interviewerListening, setInterviewerListening] = useState(false);
  const [topperExpanded, setTopperExpanded] = useState(false);
  const [flippedFeedbackCards, setFlippedFeedbackCards] = useState({});
  const [lastMainView, setLastMainView] = useState('evaluation');
  const [chatWindow, setChatWindow] = useState({ x: 0, y: 0, width: 390, height: 540 });
  const [answerUploadNames, setAnswerUploadNames] = useState([]);
  const [referenceUploadNames, setReferenceUploadNames] = useState([]);
  const activeCompareKey = useRef('');
  const askQuestionFileInputRef = useRef(null);
  const chatScrollRef = useRef(null);
  const chatDragRef = useRef(null);
  const chatResizeRef = useRef(null);
  const interviewRecognitionRef = useRef(null);
  const interviewerWaitTimeoutRef = useRef(null);
  const interviewInputFocusedRef = useRef(false);
  const interviewerHandsFreeRef = useRef(false);
  const interviewerTranscriptRef = useRef('');
  const chatModeRef = useRef(chatMode);
  const interviewStartedRef = useRef(interviewStarted);
  const chatLoadingRef = useRef(chatLoading);

  const examDetails = useMemo(() => {
    return [examBoard, subject, questionText].filter(Boolean).join(' | ');
  }, [examBoard, questionText, subject]);

  const referenceLookupDetails = useMemo(() => {
    return [examBoard, subject].filter(Boolean).join(' | ');
  }, [examBoard, subject]);

  const activeChatMessages = useMemo(
    () => (chatMode === 'interviewer' ? interviewerMessages : autoChatMessages),
    [chatMode, interviewerMessages, autoChatMessages]
  );

  const feedbackColumns = useMemo(() => {
    const grouped = {
      Strength: [],
      'Missing point': [],
      Suggestion: [],
    };

    feedbackBlocks.forEach((item, index) => {
      const bucket = grouped[item.type] ? item.type : 'Suggestion';
      grouped[bucket].push({
        ...item,
        questionLabel: extractFeedbackQuestionLabel(item.text, index),
        cardText: cleanFeedbackCardText(item.text, item.type),
      });
    });

    return grouped;
  }, [feedbackBlocks]);

  const speechRecognitionSupported = useMemo(() => {
    if (typeof window === 'undefined') return false;
    return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
  }, []);

  const examSearchSuggestions = useMemo(() => {
    const query = examBoard.trim().toLowerCase();
    if (!query) return examSearchHistory.slice(0, 6);
    return examSearchHistory
      .filter((item) => item.toLowerCase().startsWith(query))
      .slice(0, 6);
  }, [examBoard, examSearchHistory]);

  const subjectSearchSuggestions = useMemo(() => {
    const query = subject.trim().toLowerCase();
    if (!query) return subjectSearchHistory.slice(0, 6);
    return subjectSearchHistory
      .filter((item) => item.toLowerCase().startsWith(query))
      .slice(0, 6);
  }, [subject, subjectSearchHistory]);

  const rememberSearch = (value, history, setHistory, key) => {
    const trimmed = value.trim();
    if (!trimmed) return;

    const nextHistory = [
      trimmed,
      ...history.filter((item) => item.toLowerCase() !== trimmed.toLowerCase()),
    ].slice(0, 12);

    setHistory(nextHistory);
    saveSearchHistory(key, nextHistory);
  };

  const chooseExamSearch = (value) => {
    setExamBoard(value);
    setExamSearchFocused(false);
    setReferenceLookupError('');
    resetResults();
  };

  const chooseSubjectSearch = (value) => {
    setSubject(value);
    setSubjectSearchFocused(false);
    setReferenceLookupError('');
    resetResults();
  };

  const handleSearchHistoryKeyDown = (event, suggestions, activeIndex, setActiveIndex, chooseValue, closeMenu) => {
    if (!suggestions.length) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % suggestions.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + suggestions.length) % suggestions.length);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      chooseValue(suggestions[activeIndex] || suggestions[0]);
    } else if (event.key === 'Escape') {
      closeMenu(false);
    }
  };

  useEffect(() => {
    setExamSearchActiveIndex(0);
  }, [examSearchSuggestions]);

  useEffect(() => {
    setSubjectSearchActiveIndex(0);
  }, [subjectSearchSuggestions]);

  useEffect(() => {
    const query = questionBankQuery.trim();
    if (currentView !== 'questionBank') return undefined;
    if (!query) {
      setQuestionBankResults(questionBankSections);
      setQuestionBankLoading(false);
      setQuestionBankError('');
      return undefined;
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(async () => {
      setQuestionBankLoading(true);
      setQuestionBankError('');

      try {
        const response = await axios.post(
          '/question-bank-search',
          {
            query,
            subject: 'All',
          },
          { signal: controller.signal }
        );

        setQuestionBankResults(response.data.sections || questionBankSections);
      } catch (error) {
        if (axios.isCancel(error) || error.code === 'ERR_CANCELED') return;
        console.error('Question bank search failed:', error);
        setQuestionBankResults(questionBankSections);
        setQuestionBankError(error.response?.data?.error || 'Could not search study resources with Selenium.');
      } finally {
        setQuestionBankLoading(false);
      }
    }, 700);

    return () => {
      controller.abort();
      window.clearTimeout(timeoutId);
    };
  }, [currentView, questionBankQuery]);

  useEffect(() => {
    if (currentView !== 'history') return undefined;

    let isMounted = true;
    const loadHistory = async () => {
      setHistoryLoading(true);
      setHistoryError('');

      try {
        const response = await axios.get('/history');
        if (!isMounted) return;
        setHistoryData({
          evaluations: response.data.evaluations || [],
          ask_messages: response.data.ask_messages || [],
          chat_sessions: response.data.chat_sessions || [],
        });
      } catch (error) {
        if (!isMounted) return;
        console.error('History load failed:', error);
        setHistoryError(error.response?.data?.error || 'Could not load history.');
      } finally {
        if (isMounted) {
          setHistoryLoading(false);
        }
      }
    };

    loadHistory();

    return () => {
      isMounted = false;
    };
  }, [currentView]);

  useEffect(() => {
    if (!chatOpen || chatMinimized) return;
    const frame = window.requestAnimationFrame(() => {
      if (chatScrollRef.current) {
        chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeChatMessages, chatOpen, chatMinimized]);

  useEffect(() => {
    return () => {
      if (interviewerWaitTimeoutRef.current) {
        window.clearTimeout(interviewerWaitTimeoutRef.current);
      }
      if (typeof window !== 'undefined' && window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  useEffect(() => {
    if (!speechRecognitionSupported || typeof window === 'undefined') return undefined;

    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new Recognition();
    recognition.lang = 'en-IN';
    recognition.continuous = false;
    recognition.interimResults = false;

    recognition.onresult = (event) => {
      const transcript = Array.from(event.results || [])
        .map((result) => result[0]?.transcript || '')
        .join(' ')
        .trim();
      if (transcript) {
        interviewerTranscriptRef.current = transcript;
        setChatInput(transcript);
      }
    };

    recognition.onend = () => {
      setInterviewerListening(false);

      const transcript = interviewerTranscriptRef.current.trim();
      if (
        transcript &&
        interviewerHandsFreeRef.current &&
        chatModeRef.current === 'interviewer' &&
        interviewStartedRef.current &&
        !chatLoadingRef.current
      ) {
        interviewerTranscriptRef.current = '';
        sendChatMessage(transcript);
        return;
      }

      if (
        interviewerHandsFreeRef.current &&
        chatModeRef.current === 'interviewer' &&
        interviewStartedRef.current &&
        !chatLoadingRef.current &&
        !interviewInputFocusedRef.current
      ) {
        window.setTimeout(() => {
          beginAutomaticInterviewListening();
        }, 300);
      }
    };

    recognition.onerror = () => {
      setInterviewerListening(false);
      if (
        interviewerHandsFreeRef.current &&
        chatModeRef.current === 'interviewer' &&
        interviewStartedRef.current &&
        !chatLoadingRef.current &&
        !interviewInputFocusedRef.current
      ) {
        window.setTimeout(() => {
          beginAutomaticInterviewListening();
        }, 500);
      }
    };
    interviewRecognitionRef.current = recognition;

    return () => {
      interviewRecognitionRef.current = null;
      try {
        recognition.stop();
      } catch {
        // ignore stop failures during cleanup
      }
    };
  }, [speechRecognitionSupported]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    setChatWindow((current) => ({
      ...current,
      x: Math.max(24, window.innerWidth - current.width - 32),
      y: Math.max(24, window.innerHeight - current.height - 32),
    }));
    return undefined;
  }, []);

  useEffect(() => {
    const handlePointerMove = (event) => {
      if (chatDragRef.current) {
        const { offsetX, offsetY, width, height } = chatDragRef.current;
        const nextX = Math.min(Math.max(8, event.clientX - offsetX), window.innerWidth - width - 8);
        const nextY = Math.min(Math.max(8, event.clientY - offsetY), window.innerHeight - height - 8);
        setChatWindow((current) => ({ ...current, x: nextX, y: nextY }));
      }

      if (chatResizeRef.current) {
        const { startX, startY, startWidth, startHeight, startLeft, startTop } = chatResizeRef.current;
        const nextWidth = Math.min(Math.max(320, startWidth + (event.clientX - startX)), window.innerWidth - startLeft - 8);
        const nextHeight = Math.min(Math.max(280, startHeight + (event.clientY - startY)), window.innerHeight - startTop - 8);
        setChatWindow((current) => ({ ...current, width: nextWidth, height: nextHeight }));
      }
    };

    const handlePointerUp = () => {
      chatDragRef.current = null;
      chatResizeRef.current = null;
    };

    window.addEventListener('mousemove', handlePointerMove);
    window.addEventListener('mouseup', handlePointerUp);

    return () => {
      window.removeEventListener('mousemove', handlePointerMove);
      window.removeEventListener('mouseup', handlePointerUp);
    };
  }, []);

  const resetResults = () => {
    setMetrics(initialMetrics);
    setFeedbackBlocks([]);
    setComparisonResult(null);
    setFlippedFeedbackCards({});
    setEvaluationDone(false);
    setCompareError('');
  };

  const getUploadLabel = (fileNames, fallbackLabel = 'Uploaded file') => {
    if (!fileNames.length) return fallbackLabel;
    if (fileNames.length === 1) return fileNames[0];
    return `${fileNames.length} files`;
  };

  const uploadSheet = async ({
    selectedFiles,
    setSelectedFile,
    setFileNames,
    setPreview,
    setText,
    setLoading,
    setProgress,
    setError,
    onSuccess,
  }) => {
    const files = Array.from(selectedFiles || []);
    if (!files.length) return;

    const firstImageFile = files.find((item) => item.type.startsWith('image/'));

    setSelectedFile(files[0]);
    if (setFileNames) {
      setFileNames(files.map((item) => item.name));
    }
    setPreview(firstImageFile ? URL.createObjectURL(firstImageFile) : '');
    setLoading(true);
    setProgress(15);
    setError('');
    resetResults();

    try {
      const extractedChunks = [];

      for (let index = 0; index < files.length; index += 1) {
        const formData = new FormData();
        formData.append('file', files[index]);

        const response = await axios.post('/upload', formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });

        const extractedText = (response.data.extracted_text || '').trim();
        if (extractedText) {
          extractedChunks.push(extractedText);
        }

        const progressValue = 15 + Math.round(((index + 1) / files.length) * 85);
        setProgress(progressValue);
      }

      if (!extractedChunks.length) {
        throw new Error('No readable text was found in selected files.');
      }

      setText(extractedChunks.join('\n\n'));
      setProgress(100);

      if (onSuccess) {
        onSuccess();
      }
    } catch (error) {
      console.error('Error uploading file:', error);
      setError(error.response?.data?.error || 'Could not extract text from the uploaded file.');
      setProgress(100);
    } finally {
      setLoading(false);
    }
  };

  const handleStepChange = (step) => {
    setCurrentStep(step);
  };

  const handleFileUpload = async (event) => {
    const selectedFiles = event.target.files;
    await uploadSheet({
      selectedFiles,
      setSelectedFile: setFile,
      setFileNames: setAnswerUploadNames,
      setPreview: setImagePreview,
      setText: setUserAnswer,
      setLoading: setOcrLoading,
      setProgress: setOcrProgress,
      setError: setOcrError,
    });
  };

  const handleReferenceFileUpload = async (event) => {
    const selectedFiles = event.target.files;
    setReferenceLookupError('');
    setReferenceResults([]);

    await uploadSheet({
      selectedFiles,
      setSelectedFile: setReferenceFile,
      setFileNames: setReferenceUploadNames,
      setPreview: setReferencePreview,
      setText: setTopperAnswer,
      setLoading: setReferenceLoading,
      setProgress: setReferenceProgress,
      setError: setReferenceUploadError,
      onSuccess: () => setReferenceSource('manual'),
    });
  };

  const searchReferenceSheet = async () => {
    if (!examBoard.trim() || !subject.trim()) {
      setReferenceLookupError('Enter exam name and subject to search the reference sheet.');
      return;
    }

    rememberSearch(examBoard, examSearchHistory, setExamSearchHistory, examSearchHistoryKey);
    rememberSearch(subject, subjectSearchHistory, setSubjectSearchHistory, subjectSearchHistoryKey);
    setFetchingTopper(true);
    setReferenceLookupError('');
    setReferenceUploadError('');
    setReferenceDownloadMessage('');
    setReferenceResults([]);
    resetResults();

    try {
      const response = await axios.post('/search-reference-sheets', {
        exam_name: examBoard.trim(),
        subject: subject.trim(),
      });

      const results = rankReferenceResults(response.data.results || [], subject.trim(), examBoard.trim());
      const topMatch = results.find((item) => item.importable) || results[0] || null;
      setReferenceResults(results);
      setTopperAnswer('');
      setReferenceSource('');
      setReferenceFile(null);
      setReferenceUploadNames([]);
      setReferencePreview('');
      setReferenceProgress(0);

      if (!results.length) {
        setReferenceLookupError("Can't find the sheet. Upload it manually.");
      } else if (topMatch) {
        await importReferenceSheet(topMatch);
      } else {
        setReferenceLookupError('Relevant results found, but no directly importable file. Use Download or upload manually.');
      }
    } catch (error) {
      console.error('Error fetching topper sheet:', error);
      setTopperAnswer('');
      setReferenceSource('');
      setReferenceResults([]);
      setReferenceLookupError(error.response?.data?.error || "Can't find the sheet. Upload it manually.");
    } finally {
      setFetchingTopper(false);
    }
  };

  const downloadReferenceSheet = async (result) => {
    setDownloadingReferenceId(result.id);
    setReferenceLookupError('');
    setReferenceDownloadMessage('');

    try {
      const response = await axios.post('/download-reference-sheet', {
        download_url: result.download_url,
        title: result.subject_name,
      });

      setReferenceDownloadMessage(`Saved ${response.data.file_name || 'reference sheet'} to downloads/reference_sheets.`);
    } catch (error) {
      console.error('Error downloading reference sheet:', error);
      setReferenceLookupError(error.response?.data?.error || 'Could not download the selected reference sheet.');
    } finally {
      setDownloadingReferenceId('');
    }
  };

  const openReferenceLink = (url) => {
    if (!url) return;
    const openedWindow = window.open(url, '_blank', 'noopener,noreferrer');
    if (!openedWindow) {
      window.location.assign(url);
    }
  };

  const importReferenceSheet = async (result) => {
    setReferenceLookupError('');
    setReferenceUploadError('');
    setReferenceLoading(true);
    setReferenceProgress(25);
    resetResults();

    try {
      setReferenceProgress(45);
      const response = await axios.post('/import-reference-sheet', {
        download_url: result.download_url,
      });

      setTopperAnswer(response.data.extracted_text || '');
      setReferenceSource('browser');
      setReferenceFile(null);
      setReferenceUploadNames([]);
      setReferencePreview('');
      setReferenceProgress(100);
    } catch (error) {
      console.error('Error importing reference sheet:', error);
      setReferenceLookupError(error.response?.data?.error || 'Could not import the selected reference sheet.');
      setReferenceProgress(0);
    } finally {
      setReferenceLoading(false);
    }
  };

  const clearAnswerUpload = () => {
    setFile(null);
    setImagePreview('');
    setUserAnswer('');
    setAnswerUploadNames([]);
    setOcrLoading(false);
    setOcrProgress(0);
    setOcrError('');
    resetResults();
  };

  const clearReferenceUpload = () => {
    setReferenceFile(null);
    setReferencePreview('');
    setTopperAnswer('');
    setReferenceUploadNames([]);
    setReferenceLoading(false);
    setReferenceProgress(0);
    setReferenceUploadError('');
    setReferenceLookupError('');
    setReferenceSource('');
    setReferenceResults([]);
    resetResults();
  };

  const uploadAskTextFile = async ({ selectedFiles, setText, setLoading }) => {
    const files = Array.from(selectedFiles || []);
    if (!files.length) return;

    setLoading(true);
    setAskQuestionError('');

    try {
      const extractedChunks = [];

      for (let index = 0; index < files.length; index += 1) {
        const formData = new FormData();
        formData.append('file', files[index]);

        const response = await axios.post('/upload', formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });

        const extractedText = (response.data.extracted_text || '').trim();
        if (extractedText) {
          extractedChunks.push(extractedText);
        }
      }

      if (!extractedChunks.length) {
        throw new Error('No readable text was found in selected files.');
      }

      setText(extractedChunks.join('\n\n'));
    } catch (error) {
      console.error('Error uploading ask-section file:', error);
      setAskQuestionError(error.response?.data?.error || 'Could not extract text from uploaded file.');
    } finally {
      setLoading(false);
    }
  };

  const handleAskQuestionPaste = async (event) => {
    const clipboardFiles = Array.from(event.clipboardData?.files || []);
    const imageFiles = clipboardFiles.filter((item) => item.type.startsWith('image/'));
    if (!imageFiles.length) return;

    event.preventDefault();
    const previews = imageFiles.map((file) => ({
      id: `${file.name || 'pasted-image'}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      name: file.name || 'Pasted image',
      file,
      url: URL.createObjectURL(file),
      type: file.type || 'image/*',
    }));
    setAskQuestionImages((current) => [...current, ...previews]);
    setAskQuestionError('');
  };

  const handleAskQuestionFilePick = (event) => {
    const selectedFiles = Array.from(event.target.files || []).filter(
      (file) => file.type.startsWith('image/') || file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')
    );
    if (!selectedFiles.length) return;

    const previews = selectedFiles.map((file) => ({
      id: `${file.name || 'uploaded-image'}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      name: file.name || 'Uploaded image',
      file,
      url: file.type.startsWith('image/') ? URL.createObjectURL(file) : '',
      type: file.type || 'application/octet-stream',
    }));
    setAskQuestionImages((current) => [...current, ...previews]);
    setAskQuestionError('');
    event.target.value = '';
  };

  const extractAskAttachmentText = async () => {
    const files = askQuestionImages.map((item) => item.file).filter(Boolean);
    if (!files.length) return '';

    const extractedChunks = [];
    for (const fileItem of files) {
      const formData = new FormData();
      formData.append('file', fileItem);

      const response = await axios.post('/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });

      const extractedText = (response.data.extracted_text || '').trim();
      if (extractedText) {
        extractedChunks.push(extractedText);
      }
    }

    return extractedChunks.join('\n\n');
  };

  const askSingleQuestion = async () => {
    if (!askQuestionText.trim()) {
      setAskQuestionError('Type one question to ask.');
      return;
    }

    setAskQuestionLoading(true);
    setAskQuestionError('');
    setAskQuestionAnswer('');

    try {
      const attachmentText = await extractAskAttachmentText();
      const effectiveAnswerText = askAnswerText.trim() || userAnswer.trim() || attachmentText;

      const response = await axios.post('/ask-single-question', {
        user_text: effectiveAnswerText,
        question_text: askQuestionText,
      });

      setAskQuestionAnswer(cleanMarkdownForDisplay(response.data.answer || ''));
    } catch (error) {
      console.error('Error asking single question:', error);
      setAskQuestionError(error.response?.data?.error || error.message || 'Could not answer that question right now.');
    } finally {
      setAskQuestionLoading(false);
    }
  };

  const resetChat = () => {
    stopInterviewerHandsFree(false);
    interviewInputFocusedRef.current = false;
    setChatMode('auto');
    setAutoChatMessages(initialChatMessages);
    setInterviewerMessages([]);
    setChatInput('');
    setChatError('');
    setActiveAutoChatSessionId(null);
    setInterviewPosition('');
    setInterviewTopic('');
    setInterviewFocus('');
    setInterviewStarted(false);
    setInterviewerListening(false);
  };

  const openChatWorkspace = () => {
    if (currentView !== 'chat') {
      setLastMainView(currentView);
    }
    setCurrentView('chat');
    setChatOpen(true);
    setChatMinimized(false);
  };

  const minimizeChatWorkspace = () => {
    setChatOpen(true);
    setChatMinimized(true);
    setCurrentView(lastMainView || 'evaluation');
  };

  const restoreChatWorkspace = () => {
    setCurrentView('chat');
    setChatOpen(true);
    setChatMinimized(false);
  };

  const dismissChatWorkspace = () => {
    stopInterviewerHandsFree(true);
    resetChat();
    setChatOpen(true);
    setChatMinimized(false);
    setCurrentView('chat');
  };

  const startDraggingChat = (event) => {
    if (chatMinimized !== true) return;
    const rect = event.currentTarget.closest('.chat-panel-floating')?.getBoundingClientRect();
    if (!rect) return;
    chatDragRef.current = {
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
    };
  };

  const startResizingChat = (event) => {
    if (chatMinimized !== true) return;
    event.preventDefault();
    event.stopPropagation();
    chatResizeRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      startWidth: chatWindow.width,
      startHeight: chatWindow.height,
      startLeft: chatWindow.x,
      startTop: chatWindow.y,
    };
  };

  const requestAssistantReply = async ({
    message,
    mode,
    history,
    userTextOverride = '',
    referenceTextOverride = '',
    comparisonContextOverride = '',
    sessionId = null,
  }) => {
    const attachmentText = await extractAskAttachmentText();
    const comparisonContext = comparisonContextOverride || buildComparisonContextSummary({
      examBoard,
      subject,
      metrics,
      feedbackBlocks,
    });

    return axios.post('/chat-assistant', {
      message,
      mode,
      session_id: sessionId,
      messages: history,
      exam_name: examBoard,
      subject,
      user_text: userTextOverride || askAnswerText.trim() || userAnswer.trim() || attachmentText,
      reference_text: referenceTextOverride || topperAnswer.trim(),
      comparison_context: comparisonContext,
    });
  };

  const startInterviewSession = async () => {
    if (!interviewPosition.trim() || !interviewTopic.trim()) {
      setChatError('Add the position and topic before starting the interview.');
      return;
    }

    setChatLoading(true);
    setChatError('');
    setInterviewerMessages([]);
    interviewerHandsFreeRef.current = true;
    interviewerTranscriptRef.current = '';
    interviewInputFocusedRef.current = false;

    const starterMessage = [
      `Start a mock interview for this candidate.`,
      `Position: ${interviewPosition.trim()}`,
      `Topic/domain: ${interviewTopic.trim()}`,
      interviewFocus.trim() ? `Focus areas: ${interviewFocus.trim()}` : '',
      'Please begin like a real interviewer: greet the candidate warmly, ask for a short introduction, then continue with one interview question at a time.',
    ]
      .filter(Boolean)
      .join('\n');

    try {
      const response = await requestAssistantReply({
        message: starterMessage,
        mode: 'interviewer',
        history: [],
      });

      const assistantMessage = {
        id: `interviewer-start-${Date.now()}`,
        role: 'assistant',
        content: cleanMarkdownForDisplay(response.data.answer || ''),
      };
      setInterviewerMessages([assistantMessage]);
      setInterviewStarted(true);
      speakInterviewerMessage(assistantMessage.content, true);
    } catch (error) {
      console.error('Error starting interview:', error);
      setChatError(error.response?.data?.error || error.message || 'Could not start the interview right now.');
    } finally {
      setChatLoading(false);
    }
  };

  const beginAutomaticInterviewListening = () => {
    if (
      !speechRecognitionSupported ||
      !interviewRecognitionRef.current ||
      !interviewerHandsFreeRef.current ||
      interviewInputFocusedRef.current ||
      interviewerListening
    ) {
      return;
    }

    interviewerTranscriptRef.current = '';
    setChatError('');
    setInterviewerListening(true);
    try {
      interviewRecognitionRef.current.start();
    } catch {
      setInterviewerListening(false);
      setChatError('Microphone could not start automatically. Allow mic access, then click the interview bar once to resume live interview.');
    }
  };

  const sendChatMessage = async (messageOverride = '') => {
    const message = (messageOverride || chatInput).trim();
    if (!message || chatLoading) return;

    if (chatMode === 'interviewer' && !interviewStarted) {
      setChatError('Start the interview first.');
      return;
    }

    const normalizedMessage = message.toLowerCase();
    const isInterviewStopCommand =
      chatMode === 'interviewer' &&
      ['stop', 'stop interview', 'end interview', 'pause interview', 'pause', 'enough'].includes(normalizedMessage);

    const nextUserMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: message,
    };

    const currentMessages = chatMode === 'interviewer' ? interviewerMessages : autoChatMessages;
    const historyForBackend = currentMessages
      .filter((item) => item.role === 'user' || item.role === 'assistant')
      .map((item) => ({
        role: item.role,
        content: item.content,
      }));

    if (chatMode === 'interviewer') {
      setInterviewerMessages((current) => [...current, nextUserMessage]);
    } else {
      setAutoChatMessages((current) => [...current, nextUserMessage]);
    }

    if (isInterviewStopCommand) {
      stopInterviewerHandsFree(true);
      interviewInputFocusedRef.current = true;
      setChatInput('');
      setChatError('');
      setInterviewerMessages((current) => [
        ...current,
        {
          id: `assistant-stop-${Date.now()}`,
          role: 'assistant',
          content: "Okay, I'll stop the live interview mode here. You can continue by typing, or restart the interview anytime.",
        },
      ]);
      return;
    }

    setChatInput('');
    interviewerTranscriptRef.current = '';
    setChatLoading(true);
    setChatError('');

    try {
      const response = await requestAssistantReply({
        message,
        mode: chatMode,
        history: historyForBackend,
        sessionId: chatMode === 'auto' ? activeAutoChatSessionId : null,
      });

      const assistantMessage = {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: cleanMarkdownForDisplay(response.data.answer || ''),
      };

      if (chatMode === 'interviewer') {
        setInterviewerMessages((current) => [...current, assistantMessage]);
        speakInterviewerMessage(assistantMessage.content, true);
      } else {
        setAutoChatMessages((current) => [...current, assistantMessage]);
        setActiveAutoChatSessionId(response.data.session_id || null);
      }
    } catch (error) {
      console.error('Error chatting with assistant:', error);
      setChatError(error.response?.data?.error || error.message || 'Could not get a reply right now.');
    } finally {
      setChatLoading(false);
    }
  };

  const handleChatInputKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendChatMessage();
    }
  };

  const switchChatMode = (modeKey) => {
    setChatMode(modeKey);
    setChatError('');
    setChatInput('');
  };

  const stopInterviewerHandsFree = (cancelSpeech = true) => {
    interviewerHandsFreeRef.current = false;
    interviewerTranscriptRef.current = '';

    if (interviewerWaitTimeoutRef.current) {
      window.clearTimeout(interviewerWaitTimeoutRef.current);
      interviewerWaitTimeoutRef.current = null;
    }

    if (interviewRecognitionRef.current) {
      try {
        interviewRecognitionRef.current.stop();
      } catch {
        // ignore recognition stop failures
      }
    }
    setInterviewerListening(false);

    if (cancelSpeech && typeof window !== 'undefined' && window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
  };

  const resumeInterviewerHandsFree = () => {
    if (chatMode !== 'interviewer' || !interviewStarted || chatLoading) return;

    interviewInputFocusedRef.current = false;
    interviewerHandsFreeRef.current = true;
    interviewerTranscriptRef.current = '';
    setChatError('');
    beginAutomaticInterviewListening();
  };

  const handleInterviewAnswerFocus = () => {
    interviewInputFocusedRef.current = true;
    stopInterviewerHandsFree(true);
  };

  const handleInterviewAnswerBlur = () => {
    interviewInputFocusedRef.current = false;
  };

  const speakInterviewerMessage = (text, restartListeningAfter = false) => {
    if (!text.trim()) return;
    if (typeof window === 'undefined' || !window.speechSynthesis) {
      if (restartListeningAfter) {
        beginAutomaticInterviewListening();
      }
      return;
    }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.onend = () => {
      if (restartListeningAfter) {
        beginAutomaticInterviewListening();
      }
    };
    utterance.onerror = () => {
      if (restartListeningAfter) {
        beginAutomaticInterviewListening();
      }
    };
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  };

  const toggleInterviewListening = () => {
    if (!speechRecognitionSupported || !interviewRecognitionRef.current) return;
    if (interviewerListening) {
      interviewRecognitionRef.current.stop();
      setInterviewerListening(false);
      return;
    }

    setChatError('');
    setInterviewerListening(true);
    try {
      interviewRecognitionRef.current.start();
    } catch (error) {
      setInterviewerListening(false);
      setChatError('Could not start microphone input in this browser.');
    }
  };

  const ensureTopperAnswer = async () => {
    if (topperAnswer.trim()) return topperAnswer.trim();
    if (!referenceLookupDetails) return '';

    setFetchingTopper(true);
    setReferenceLookupError('');
    try {
      const response = await axios.post('/fetch-topper-sheet', {
        exam_details: referenceLookupDetails,
      });

      const fetchedTopper = response.data.topper_text || '';
      setTopperAnswer(fetchedTopper);
      setReferenceSource('auto');
      return fetchedTopper;
    } catch (error) {
      console.error('Error fetching topper sheet:', error);
      setReferenceLookupError("Can't find the sheet. Upload it manually.");
      return '';
    } finally {
      setFetchingTopper(false);
    }
  };

  const runComparison = async ({ stayOnResultsPage = false } = {}) => {
    if (compareLoading || activeCompareKey.current) {
      return;
    }

    if (!userAnswer.trim()) {
      setCompareError('Add your answer first by uploading an image or pasting text.');
      if (!stayOnResultsPage) {
        setCurrentStep(1);
      }
      return;
    }

    if (!topperAnswer.trim() && !referenceLookupDetails) {
      setCompareError('Add exam name and subject or upload a reference sheet before running evaluation.');
      if (!stayOnResultsPage) {
        setCurrentStep(1);
      }
      return;
    }

    activeCompareKey.current = 'pending';
    setCurrentStep(3);
    setCompareLoading(true);
    setEvaluationDone(false);
    setCompareError('');

    const resolvedTopper = await ensureTopperAnswer();
    if (!resolvedTopper) {
      activeCompareKey.current = '';
      setCompareLoading(false);
      setCompareError('Could not generate a reference answer from the backend.');
      return;
    }

    try {
      const compareMaxMarks = Number.parseFloat(maxMarks);
      const effectiveMaxMarks = Number.isFinite(compareMaxMarks) && compareMaxMarks > 0 ? compareMaxMarks : 5;
      const compareKey = JSON.stringify({
        userAnswer: userAnswer.trim(),
        topperAnswer: resolvedTopper.trim(),
        maxMarks: effectiveMaxMarks,
      });

      activeCompareKey.current = compareKey;
      const response = await axios.post('/compare', {
        user_text: userAnswer,
        topper_text: resolvedTopper,
        max_marks: effectiveMaxMarks,
        exam_name: examBoard,
        subject,
      });

      const feedbackText = response.data.feedback || '';
      const parsedBlocks = splitFeedback(feedbackText);
      const fallbackBlocks = buildFallbackFeedback(userAnswer, resolvedTopper, questionText);
      const finalBlocks = parsedBlocks.length ? parsedBlocks : fallbackBlocks;
      const backendMetrics = response.data.metrics || {};
      const coverage = Number.isFinite(backendMetrics.coverage)
        ? Math.round(backendMetrics.coverage)
        : Math.min(100, Math.max(35, Math.round((userAnswer.trim().length / Math.max(resolvedTopper.trim().length, 1)) * 100)));
      const accuracy = Number.isFinite(backendMetrics.accuracy)
        ? Math.round(backendMetrics.accuracy)
        : Math.min(96, Math.max(45, parsedBlocks.length ? 84 : 72));
      const overall = Number.isFinite(backendMetrics.overall)
        ? Math.round(backendMetrics.overall)
        : Math.round((coverage + accuracy) / 2);
      const scoreValue = Math.max(0, Math.min(100, overall));
      setMetrics({
        scoreText: `${scoreValue}/100`,
        scoreDetail: '',
        coverageText: `${coverage}%`,
        accuracyText: `${accuracy}%`,
        scoreClass: getMetricClass(scoreValue),
        coverageClass: getMetricClass(coverage),
        accuracyClass: getMetricClass(accuracy),
      });
      setComparisonResult(response.data || null);
      setFeedbackBlocks(finalBlocks);
      setEvaluationDone(true);
    } catch (error) {
      console.error('Error comparing texts:', error);

      if (error.response?.status === 400) {
        setMetrics(initialMetrics);
        setFeedbackBlocks([]);
        setEvaluationDone(false);
        setCompareError(error.response?.data?.error || 'These files cannot be compared fairly.');
        return;
      }

      setMetrics(initialMetrics);
      setFeedbackBlocks([]);
      setEvaluationDone(false);
      const rawErrorPayload = error.response?.data;
      const backendError =
        (rawErrorPayload && typeof rawErrorPayload === 'object' && rawErrorPayload.error)
          ? rawErrorPayload.error
          : (typeof rawErrorPayload === 'string'
              ? rawErrorPayload.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 260)
              : '');
      setCompareError(
        backendError
          ? `Live AI feedback failed: ${backendError}`
          : 'Live AI feedback is unavailable right now. Check backend terminal logs for the exact error.'
      );
    } finally {
      activeCompareKey.current = '';
      setCompareLoading(false);
    }
  };

  const openHistoryEvaluation = async (evaluationId) => {
    setHistoryOpeningId(`evaluation-${evaluationId}`);
    setHistoryError('');

    try {
      const response = await axios.get(`/history/evaluations/${evaluationId}`);
      const detail = response.data || {};
      const restored = buildMetricsFromHistory(detail.result_payload || {}, detail.user_text || '', detail.topper_text || '');

      setExamBoard(detail.exam_name || '');
      setSubject(detail.subject || '');
      setUserAnswer(detail.user_text || '');
      setTopperAnswer(detail.topper_text || '');
      setMaxMarks(detail.max_marks ? String(detail.max_marks) : '');
      setMetrics(restored.metrics);
      setComparisonResult(detail.result_payload || null);
      setFeedbackBlocks(restored.feedbackBlocks);
      setCompareError('');
      setEvaluationDone(true);
      setCurrentView('evaluation');
      setCurrentStep(3);
    } catch (error) {
      console.error('History evaluation load failed:', error);
      setHistoryError(error.response?.data?.error || 'Could not open that comparison.');
    } finally {
      setHistoryOpeningId('');
    }
  };

  const openHistoryChatSession = async (sessionId) => {
    setHistoryOpeningId(`chat-${sessionId}`);
    setHistoryError('');

    try {
      const response = await axios.get(`/history/chat-sessions/${sessionId}`);
      const detail = response.data || {};
      const restoredMessages = Array.isArray(detail.messages) && detail.messages.length
        ? detail.messages.map((item, index) => ({
            id: `history-chat-${detail.id || sessionId}-${index}`,
            role: item.role === 'user' ? 'user' : 'assistant',
            content: cleanMarkdownForDisplay(item.content || ''),
          }))
        : initialChatMessages;

      stopInterviewerHandsFree(true);
      setChatMode('auto');
      setAutoChatMessages(restoredMessages);
      setInterviewerMessages([]);
      setChatInput('');
      setChatError('');
      setInterviewStarted(false);
      setInterviewerListening(false);
      setActiveAutoChatSessionId(detail.id || sessionId);
      setChatOpen(true);
      setChatMinimized(false);
      setCurrentView('chat');
    } catch (error) {
      console.error('History chat-session load failed:', error);
      setHistoryError(error.response?.data?.error || 'Could not open that saved chat.');
    } finally {
      setHistoryOpeningId('');
    }
  };

  const openHistoryAskMessage = async (askMessageId) => {
    setHistoryOpeningId(`ask-${askMessageId}`);
    setHistoryError('');

    try {
      const response = await axios.get(`/history/ask-messages/${askMessageId}`);
      const detail = response.data || {};

      setChatMode('auto');
      setActiveAutoChatSessionId(null);
      setAutoChatMessages([
        initialChatMessages[0],
        {
          id: `history-user-${detail.id || askMessageId}`,
          role: 'user',
          content: detail.question_text || '',
        },
        {
          id: `history-assistant-${detail.id || askMessageId}`,
          role: 'assistant',
          content: cleanMarkdownForDisplay(detail.answer_text || ''),
        },
      ]);
      setChatInput('');
      setChatError('');
      setChatOpen(true);
      setChatMinimized(false);
      setCurrentView('chat');
    } catch (error) {
      console.error('History ask-message load failed:', error);
      setHistoryError(error.response?.data?.error || 'Could not open that saved question.');
    } finally {
      setHistoryOpeningId('');
    }
  };

  const resetAll = () => {
    setCurrentStep(1);
    setFile(null);
    setImagePreview('');
    setReferenceFile(null);
    setReferencePreview('');
    setUserAnswer('');
    setTopperAnswer('');
    setExamBoard('');
    setSubject('');
    setQuestionText('');
    setMaxMarks('');
    setOcrLoading(false);
    setOcrProgress(0);
    setOcrError('');
    setReferenceLoading(false);
    setReferenceProgress(0);
    setReferenceUploadError('');
    setReferenceLookupError('');
    setReferenceSource('');
    setReferenceUploadNames([]);
    setFetchingTopper(false);
    setCompareLoading(false);
    setCompareError('');
    setMetrics(initialMetrics);
    setFeedbackBlocks([]);
    setEvaluationDone(false);
    setAskQuestionText('');
    setAskAnswerText('');
    setAskQuestionImages([]);
    setAskQuestionLoading(false);
    setAskQuestionUploadLoading(false);
    setAskAnswerUploadLoading(false);
    setAskQuestionError('');
    setAskQuestionAnswer('');
    setAnswerUploadNames([]);
    setReferenceUploadNames([]);
  };

  const comparisonInsights = useMemo(
    () => buildComparisonInsights(comparisonResult, feedbackBlocks, topperAnswer),
    [comparisonResult, feedbackBlocks, topperAnswer]
  );
  const latestInterviewerAssistantId = useMemo(
    () => [...interviewerMessages].reverse().find((item) => item.role === 'assistant')?.id || '',
    [interviewerMessages]
  );

  useEffect(() => {
    chatModeRef.current = chatMode;
  }, [chatMode]);

  useEffect(() => {
    interviewStartedRef.current = interviewStarted;
  }, [interviewStarted]);

  useEffect(() => {
    chatLoadingRef.current = chatLoading;
  }, [chatLoading]);

  useEffect(() => {
    setFlippedFeedbackCards({});
  }, [feedbackBlocks]);

  useEffect(() => {
    if (interviewerWaitTimeoutRef.current) {
      window.clearTimeout(interviewerWaitTimeoutRef.current);
      interviewerWaitTimeoutRef.current = null;
    }

    if (chatMode !== 'interviewer' || !interviewStarted || chatLoading || !interviewerHandsFreeRef.current) return undefined;
    const latestMessage = interviewerMessages[interviewerMessages.length - 1];
    if (!latestMessage || latestMessage.role !== 'assistant') return undefined;

    interviewerWaitTimeoutRef.current = window.setTimeout(() => {
      if (chatInput.trim()) return;
      const reminder = {
        id: `interviewer-wait-${Date.now()}`,
        role: 'assistant',
        content: "I'm waiting for your answer whenever you're ready.",
      };
      setInterviewerMessages((current) => {
        const currentLatest = current[current.length - 1];
        if (!currentLatest || currentLatest.id !== latestMessage.id) return current;
        return [...current, reminder];
      });
      if (!interviewInputFocusedRef.current) {
        speakInterviewerMessage(reminder.content);
      }
    }, 60000);

    return () => {
      if (interviewerWaitTimeoutRef.current) {
        window.clearTimeout(interviewerWaitTimeoutRef.current);
        interviewerWaitTimeoutRef.current = null;
      }
    };
  }, [chatInput, chatLoading, chatMode, interviewStarted, interviewerMessages]);
  const flatQuestionBankResults = useMemo(
    () =>
      questionBankResults.flatMap((section) =>
        (section.items || []).map((item) => ({
          ...item,
          sectionTitle: section.title,
        }))
      ),
    [questionBankResults]
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="logo">
          <div className="logo-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="#E6F1FB" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </div>
          <div className="logo-copy">
            <div className="logo-name">EvalAI</div>
            <span>Exam Evaluator</span>
          </div>
        </div>

        <div className="nav-section">MAIN</div>
        {sidebarItems.map((item, index) => (
          <button
            key={item}
            type="button"
            className={`nav-item ${currentView === 'evaluation' && index === 0 ? 'active' : ''}`}
            onClick={() => {
              if (index === 0) {
                setLastMainView('evaluation');
                setCurrentView('evaluation');
              }
            }}
          >
            {item}
          </button>
        ))}

        <div className="nav-section tools">TOOLS</div>
        {toolItems.map((item) => (
          <button
            key={item}
            type="button"
            className={`nav-item ${
              (currentView === 'questionBank' && item === 'Question bank') || (currentView === 'history' && item === 'History')
                || (currentView === 'chat' && item === 'Chat')
                ? 'active'
                : ''
            }`}
            onClick={() => {
              if (item === 'Question bank') {
                setLastMainView('questionBank');
                setCurrentView('questionBank');
              } else if (item === 'History') {
                setLastMainView('history');
                setCurrentView('history');
              } else if (item === 'Chat') {
                openChatWorkspace();
              }
            }}
          >
            {item}
          </button>
        ))}

        <div className="sidebar-footer">
          <div className="avatar">AC</div>
          <div>
            <div className="sidebar-user-name">Ananda Chaturvedi</div>
            <div className="sidebar-user-sub">Student</div>
          </div>
        </div>
      </aside>

      <main className={`main-content ${currentView === 'chat' ? 'chat-view' : ''}`}>
        <div className="page-header">
          <div className="page-title">{currentView === 'questionBank' || currentView === 'history' || currentView === 'chat' ? '' : 'Exam answer evaluator'}</div>
          <div className="page-sub">
            {currentView === 'questionBank' || currentView === 'history' || currentView === 'chat'
              ? ''
              : 'Upload your answer, compare with a topper sheet, and get AI-powered feedback.'}
          </div>
        </div>

        {currentView === 'evaluation' && (
        <div className="steps">
          {[1, 3].map((step) => {
            const statusClass = currentStep === step ? 'active' : currentStep > step ? 'done' : '';

            return (
              <button key={step} type="button" className={`step ${statusClass}`} onClick={() => handleStepChange(step)}>
                <div className="step-num">STEP {step}</div>
                <div className="step-label">
                  {step === 1 && 'Upload answer'}
                  {step === 3 && 'Final feedback'}
                </div>
              </button>
            );
          })}
        </div>
        )}

        {currentView === 'evaluation' && currentStep === 1 && (
          <section className="panel active">
              <div className="card">
              <div className="card-title">Upload reference sheet</div>
              <p className="note">Search from browser or upload a file manually.</p>
              <div className="mode-toggle">
                <button
                  type="button"
                  className={`mode-toggle-btn ${referenceMode === 'search' ? 'active' : ''}`}
                  onClick={() => {
                    setReferenceMode('search');
                    setReferenceLookupError('');
                  }}
                >
                  Search
                </button>
                <button
                  type="button"
                  className={`mode-toggle-btn ${referenceMode === 'upload' ? 'active' : ''}`}
                  onClick={() => {
                    setReferenceMode('upload');
                    setReferenceLookupError('');
                  }}
                >
                  Upload
                </button>
              </div>

              {referenceMode === 'search' ? (
                <div className="search-panel">
                  <div className="two-col label-fields">
                    <div>
                      <label className="field-label" htmlFor="step1-exam-name">Exam name</label>
                      <div className="search-history-field">
                        <input
                          id="step1-exam-name"
                          type="text"
                          value={examBoard}
                          autoComplete="off"
                          onFocus={() => setExamSearchFocused(true)}
                          onBlur={() => window.setTimeout(() => setExamSearchFocused(false), 120)}
                          onKeyDown={(event) =>
                            handleSearchHistoryKeyDown(
                              event,
                              examSearchSuggestions,
                              examSearchActiveIndex,
                              setExamSearchActiveIndex,
                              chooseExamSearch,
                              setExamSearchFocused
                            )
                          }
                          onChange={(event) => {
                            setExamBoard(event.target.value);
                            setReferenceLookupError('');
                            resetResults();
                          }}
                          placeholder="e.g. CBSE Class 12"
                        />
                        {examSearchFocused && examSearchSuggestions.length > 0 ? (
                          <div className="search-history-menu">
                            {examSearchSuggestions.map((item, index) => (
                              <button
                                type="button"
                                className={`search-history-item ${index === examSearchActiveIndex ? 'active' : ''}`}
                                key={item}
                                onMouseEnter={() => setExamSearchActiveIndex(index)}
                                onMouseDown={(event) => {
                                  event.preventDefault();
                                  chooseExamSearch(item);
                                }}
                              >
                                {item}
                              </button>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                    <div>
                      <label className="field-label" htmlFor="step1-subject-name">Subject</label>
                      <div className="search-history-field">
                        <input
                          id="step1-subject-name"
                          type="text"
                          value={subject}
                          autoComplete="off"
                          onFocus={() => setSubjectSearchFocused(true)}
                          onBlur={() => window.setTimeout(() => setSubjectSearchFocused(false), 120)}
                          onKeyDown={(event) =>
                            handleSearchHistoryKeyDown(
                              event,
                              subjectSearchSuggestions,
                              subjectSearchActiveIndex,
                              setSubjectSearchActiveIndex,
                              chooseSubjectSearch,
                              setSubjectSearchFocused
                            )
                          }
                          onChange={(event) => {
                            setSubject(event.target.value);
                            setReferenceLookupError('');
                            resetResults();
                          }}
                          placeholder="e.g. Physics"
                        />
                        {subjectSearchFocused && subjectSearchSuggestions.length > 0 ? (
                          <div className="search-history-menu">
                            {subjectSearchSuggestions.map((item, index) => (
                              <button
                                type="button"
                                className={`search-history-item ${index === subjectSearchActiveIndex ? 'active' : ''}`}
                                key={item}
                                onMouseEnter={() => setSubjectSearchActiveIndex(index)}
                                onMouseDown={(event) => {
                                  event.preventDefault();
                                  chooseSubjectSearch(item);
                                }}
                              >
                                {item}
                              </button>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </div>
                  <button type="button" className="btn btn-primary search-btn" onClick={searchReferenceSheet} disabled={fetchingTopper}>
                    {fetchingTopper ? 'Searching...' : 'Search reference sheet'}
                  </button>
                </div>
              ) : (
                <>
                  <label className="upload-zone" htmlFor="reference-file-input-step1">
                    <svg viewBox="0 0 24 24">
                      <rect x="3" y="3" width="18" height="18" rx="2" />
                      <path d="M9 12h6M12 9v6" />
                    </svg>
                    <p>Click to upload a photo or scan of your reference sheet</p>
                    <span>JPG, PNG, PDF, handwritten or printed</span>
                  </label>
                  <input
                    id="reference-file-input-step1"
                    type="file"
                    accept="image/*,.pdf,application/pdf"
                    multiple
                    onChange={handleReferenceFileUpload}
                  />
                  {(referencePreview || referenceFile || referenceUploadNames.length > 0 || referenceUploadError || topperAnswer || referenceLoading) && (
                    <div className="preview-section">
                      {referencePreview && <img className="img-preview" src={referencePreview} alt="Reference preview" />}
                      <div className="row-between">
                        <span className="field-label inline-label">Extracted topper answer text</span>
                        <button
                          type="button"
                          className="chip chip-blue chip-action"
                          onClick={clearReferenceUpload}
                          aria-label="Remove reference sheet"
                        >
                          {getUploadLabel(referenceUploadNames, referenceFile ? referenceFile.name : 'Reference sheet')} x
                        </button>
                      </div>
                      <div className="loading-bar">
                        <div className="loading-bar-inner" style={{ width: `${referenceProgress}%` }} />
                      </div>
                      <div className="ocr-preview">
                        {referenceUploadError || (referenceLoading ? 'Processing uploaded reference sheet...' : topperAnswer || 'No text extracted yet.')}
                      </div>
                    </div>
                  )}
                </>
              )}

              {fetchingTopper && referenceMode === 'search' && (
                <p className="status-text">Searching the web with Selenium. This can take a minute or two...</p>
              )}

              {!fetchingTopper && referenceMode === 'search' && referenceResults.length > 0 && (
                <div className="search-results">
                  {referenceResults.map((result) => (
                    <div key={result.id} className="search-result-card">
                      <div className="search-result-copy">
                        <div className="search-result-title">{result.subject_name}</div>
                        <div className="search-result-meta">
                          {result.source} | Class {result.class_name || '-'} | {result.year || '-'} | {result.file_type} | {result.file_size || '-'}
                        </div>
                      </div>
                      <div className="search-result-actions">
                        {result.importable ? (
                          <button
                            type="button"
                            className="btn"
                            onClick={() => downloadReferenceSheet(result)}
                            disabled={downloadingReferenceId === result.id}
                          >
                            {downloadingReferenceId === result.id ? 'Downloading...' : 'Download'}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn"
                            onClick={() => openReferenceLink(result.download_url)}
                          >
                            Open
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {referenceDownloadMessage && <p className="status-text">{referenceDownloadMessage}</p>}
              {referenceLookupError && <p className="small-error">{referenceLookupError}</p>}
            </div>

            <div className="card">
              <div className="card-title">Upload your answer</div>
              <label className="upload-zone compact" htmlFor="answer-file-input">
                <svg viewBox="0 0 24 24">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <path d="M9 12h6M12 9v6" />
                </svg>
                <p>Click to upload your answer sheet</p>
                <span>JPG, PNG, PDF</span>
              </label>
              <input
                id="answer-file-input"
                type="file"
                accept="image/*,.pdf,application/pdf"
                multiple
                onChange={handleFileUpload}
              />
              {(imagePreview || file || ocrError) && (
                <div className="preview-section">
                  {imagePreview && <img className="img-preview" src={imagePreview} alt="Answer preview" />}
                  <div className="row-between">
                    <span className="field-label inline-label">Extracted answer text</span>
                    <button
                      type="button"
                      className="chip chip-blue chip-action"
                      onClick={clearAnswerUpload}
                      aria-label="Remove answer sheet"
                    >
                      {getUploadLabel(answerUploadNames, file ? file.name : 'Uploaded file')} x
                    </button>
                  </div>
                  <div className="loading-bar">
                    <div className="loading-bar-inner" style={{ width: `${ocrProgress}%` }} />
                  </div>
                  <div className="ocr-preview">
                    {ocrError || (ocrLoading ? 'Processing uploaded file...' : userAnswer || 'No text extracted yet.')}
                  </div>
                </div>
              )}
            </div>

            <div className="first-page-actions">
              <button
                type="button"
                className="btn btn-primary compare-btn"
                onClick={() => runComparison()}
                disabled={!userAnswer.trim()}
              >
                Compare answers
              </button>
            </div>

          </section>
        )}

        {currentView === 'evaluation' && currentStep === 3 && (
          <section className="panel active">
            {compareLoading && (
              <div className="card">
                <div className="card-title">Evaluating your answer...</div>
                <div className="loading-bar">
                  <div className="loading-bar-inner animated" />
                </div>
                <p className="note">Comparing with model answer, analysing coverage, and generating feedback.</p>
              </div>
            )}

            {!compareLoading && !evaluationDone && (
              <div className="results-empty">Complete 1 and 2 to get final feedback.</div>
            )}

            {!compareLoading && evaluationDone && (
              <>
                <div className="score-grid">
                  <div className="metric">
                    <div className="metric-label">Score</div>
                    <div className={`metric-val ${metrics.scoreClass}`}>{metrics.scoreText}</div>
                  </div>
                  <div className="metric">
                    <div className="metric-label">Coverage</div>
                    <div className={`metric-val ${metrics.coverageClass}`}>{metrics.coverageText}</div>
                  </div>
                  <div className="metric">
                    <div className="metric-label">Accuracy</div>
                    <div className={`metric-val ${metrics.accuracyClass}`}>{metrics.accuracyText}</div>
                  </div>
                </div>

                <div className="feedback-section">
                  <div className="feedback-flash-grid">
                    {Object.entries(feedbackColumns).map(([type, cards]) => (
                      <div key={type} className="feedback-column">
                        <div className="fb-type">{type}</div>
                        {cards.length ? (
                          <div className="feedback-flash-stack">
                            {cards.map((item) => {
                              const isFlipped = Boolean(flippedFeedbackCards[item.id]);
                              return (
                                <button
                                  key={item.id}
                                  type="button"
                                  className={`feedback-flashcard ${item.cls} ${isFlipped ? 'flipped' : ''}`}
                                  onClick={() =>
                                    setFlippedFeedbackCards((current) => ({
                                      ...current,
                                      [item.id]: !current[item.id],
                                    }))
                                  }
                                  aria-pressed={isFlipped}
                                >
                                  <div className="feedback-flashcard-inner">
                                    <div className="feedback-flashcard-face feedback-flashcard-front">
                                      {item.type === 'Strength' ? (
                                        <span className="feedback-strength-icon" aria-hidden="true">
                                          <svg viewBox="0 0 64 64">
                                            <path
                                              d="M8 54h48"
                                              fill="none"
                                              stroke="currentColor"
                                              strokeWidth="3.5"
                                              strokeLinecap="round"
                                            />
                                            <rect x="15" y="34" width="8" height="20" rx="1.5" fill="currentColor" />
                                            <rect x="28" y="25" width="8" height="29" rx="1.5" fill="currentColor" />
                                            <rect x="41" y="18" width="8" height="36" rx="1.5" fill="currentColor" />
                                            <path
                                              d="M32 6l3.4 6.8 7.5 1.1-5.4 5.2 1.3 7.4L32 22.9l-6.8 3.6 1.3-7.4-5.4-5.2 7.5-1.1L32 6z"
                                              fill="currentColor"
                                            />
                                            <path
                                              d="M12 17l2.2 2.2M52 17l-2.2 2.2M18 10l1.2 3.2M46 10l-1.2 3.2"
                                              fill="none"
                                              stroke="currentColor"
                                              strokeWidth="3"
                                              strokeLinecap="round"
                                            />
                                            <path
                                              d="M32 11.5l1.2 2.5 2.8.4-2 1.9.5 2.8-2.5-1.3-2.5 1.3.5-2.8-2-1.9 2.8-.4L32 11.5z"
                                              fill="#fff"
                                            />
                                          </svg>
                                        </span>
                                      ) : null}
                                      {item.questionLabel ? (
                                        <span className="feedback-question-label">{item.questionLabel}</span>
                                      ) : null}
                                    </div>
                                    <div className="feedback-flashcard-face feedback-flashcard-back">
                                      <p>{item.cardText}</p>
                                    </div>
                                  </div>
                                </button>
                              );
                            })}
                          </div>
                        ) : (
                          <div className="feedback-column-empty">No {type.toLowerCase()} yet.</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>

                <div className="insight-grid">
                  <div className="card insight-card">
                    <div className="card-title">Highest marks</div>
                    <div className="insight-title">{comparisonInsights.highestYield.title}</div>
                    <p className="insight-text">{comparisonInsights.highestYield.text}</p>
                  </div>

                  <div className="card insight-card">
                    <div className="card-title">You struggle with</div>
                    <ul className="revision-list">
                      {comparisonInsights.struggleAreas.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </div>
                </div>

                <div className={`card topper-card${topperExpanded ? ' expanded' : ''}`}>
                  <button
                    type="button"
                    className="topper-toggle"
                    onClick={() => setTopperExpanded((current) => !current)}
                    aria-expanded={topperExpanded}
                  >
                    <span className="card-title">Topper's answer (reference)</span>
                  </button>
                  {topperExpanded ? (
                    <div className="topper-box">{topperAnswer || 'No topper answer was returned by the backend.'}</div>
                  ) : null}
                </div>
              </>
            )}

            {compareError && <div className="error-banner">{compareError}</div>}

          </section>
        )}

        {currentView === 'questionBank' && (
          <section className="panel active">
            <div className="question-bank-hero">
              <div>
                <h2>Fast Study Shelf</h2>
              </div>
              <div className="question-bank-badges">
                <span className="chip chip-blue">Notes</span>
                <span className="chip chip-blue">PYQs</span>
                <span className="chip chip-blue">Books</span>
              </div>
            </div>

            <div className="card">
              <div className="card-title">Search library</div>
              <input
                type="text"
                value={questionBankQuery}
                onChange={(event) => setQuestionBankQuery(event.target.value)}
                placeholder="Search topic, chapter, exam, or book..."
              />
              {questionBankLoading && <p className="status-text">Searching Bing and DuckDuckGo with Selenium...</p>}
              {questionBankError && <p className="small-error">{questionBankError}</p>}
            </div>

            {flatQuestionBankResults.length ? (
              <div className="card question-bank-results-card">
                <div className="question-bank-section-head">
                  <div className="card-title">Results</div>
                  <span className="resource-count">{flatQuestionBankResults.length}</span>
                </div>
                <div className="resource-list">
                  {flatQuestionBankResults.map((item) => (
                    <div key={`${item.sectionTitle}-${item.id}`} className="resource-item">
                      <div>
                        <div className="resource-title">{item.title}</div>
                        <div className="resource-meta">{item.sectionTitle} | {item.subject} | {item.meta}</div>
                      </div>
                      <a className="btn" href={item.download_url} target="_blank" rel="noreferrer">
                        Download
                      </a>
                    </div>
                  ))}
                </div>
              </div>
            ) : questionBankQuery.trim() ? (
              <div className="card question-bank-results-card">
                <div className="question-bank-empty">
                  No Selenium results yet for this search.
                </div>
              </div>
            ) : null}
          </section>
        )}

        {currentView === 'history' && (
          <section className="panel active">
            <div className="history-hero">
              <div>
                <h2>History</h2>
              </div>
              <button type="button" className="btn" onClick={() => setCurrentView('evaluation')}>
                New evaluation
              </button>
            </div>

            {historyLoading && <p className="status-text">Loading history...</p>}
            {historyError && <p className="small-error">{historyError}</p>}

            <div className="history-grid">
              <div className="card history-card">
                <div className="history-section-head">
                  <div className="card-title">Compared sheets</div>
                </div>
                {historyData.evaluations.length ? (
                  <div className="history-list">
                    {historyData.evaluations.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className="history-item history-item-button"
                        onClick={() => openHistoryEvaluation(item.id)}
                        disabled={historyOpeningId === `evaluation-${item.id}`}
                      >
                        <div className="history-item-top">
                          <div>
                            <div className="history-title">{item.label}</div>
                            <div className="history-meta">
                              {item.percentage ?? '-'}% | {item.earned_marks ?? '-'} / {item.max_marks ?? '-'} marks
                            </div>
                          </div>
                          <span className="history-date">
                            {historyOpeningId === `evaluation-${item.id}` ? 'Opening...' : formatHistoryDate(item.created_at)}
                          </span>
                        </div>
                        {item.feedback_preview && <p className="history-preview">{item.feedback_preview}</p>}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="question-bank-empty">No compared sheets saved yet.</div>
                )}
              </div>

              <div className="card history-card">
                <div className="history-section-head">
                  <div className="card-title">Chat history</div>
                </div>
                {historyData.chat_sessions.length ? (
                  <div className="history-list">
                    {historyData.chat_sessions.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className="history-item history-item-button"
                        onClick={() => openHistoryChatSession(item.id)}
                        disabled={historyOpeningId === `chat-${item.id}`}
                      >
                        <div className="history-item-top">
                          <div>
                            <div className="history-title">{item.title}</div>
                            <div className="history-meta">
                              {item.message_count ?? 0} messages
                            </div>
                          </div>
                          <span className="history-date">
                            {historyOpeningId === `chat-${item.id}` ? 'Opening...' : formatHistoryDate(item.updated_at || item.created_at)}
                          </span>
                        </div>
                        {item.preview ? <p className="history-preview">{item.preview}</p> : null}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="question-bank-empty">No chat threads saved yet.</div>
                )}
              </div>
            </div>
          </section>
        )}

        {currentView === 'chat' && (
          <section className="panel active chat-panel-shell">
            <div className="chat-page card">
              <div className="chat-panel-head">
                <div>
                  <div className="chat-panel-title">EvalAI Chat</div>
                </div>
                <div className="chat-panel-actions">
                  {chatMode === 'interviewer' ? (
                    <button type="button" className="chat-dismiss-btn" onClick={dismissChatWorkspace}>
                      Dismiss
                    </button>
                  ) : (
                    <button type="button" className="chat-dismiss-btn" onClick={() => switchChatMode('interviewer')}>
                      Interviewer
                    </button>
                  )}
                  <button type="button" className="chat-head-btn" onClick={minimizeChatWorkspace} title="Minimize chat" aria-label="Minimize chat">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path
                        d="M7 9.5V7h2.5M14.5 7H17v2.5M17 14.5V17h-2.5M9.5 17H7v-2.5"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.9"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </button>
                </div>
              </div>

              {chatMode === 'auto' ? (
                <>
                  <div className="chat-thread chat-thread-page" ref={chatScrollRef}>
                    {autoChatMessages.map((item) => (
                      <div key={item.id} className={`chat-bubble-row ${item.role === 'user' ? 'user' : 'assistant'}`}>
                        <div className={`chat-bubble ${item.role === 'user' ? 'user' : 'assistant'}`}>{item.content}</div>
                      </div>
                    ))}
                    {chatLoading ? (
                      <div className="chat-bubble-row assistant">
                        <div className="chat-bubble assistant chat-bubble-loading">Thinking...</div>
                      </div>
                    ) : null}
                  </div>

                  {chatError ? <div className="small-error chat-error">{chatError}</div> : null}

                  <div className="chat-compose">
                    <div className="chat-compose-box">
                      <textarea
                        value={chatInput}
                        onChange={(event) => setChatInput(event.target.value)}
                        onKeyDown={handleChatInputKeyDown}
                        placeholder="Ask anything, ask for interview practice, or ask about your current sheet..."
                      />
                      <div className="chat-compose-actions">
                        <button
                          type="button"
                          className="chat-compose-icon-btn chat-send-btn"
                          onClick={sendChatMessage}
                          disabled={chatLoading || !chatInput.trim()}
                          aria-label="Send message"
                          title="Send message"
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true">
                            <path
                              d="M12 18V6M12 6l-5 5M12 6l5 5"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2.1"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                      </div>
                    </div>
                  </div>
                </>
              ) : (
                <>
                  {!interviewStarted ? (
                    <div className="interview-setup">
                      <div className="interview-setup-copy">
                        <div className="insight-title">Set up your mock interview</div>
                      </div>
                      <div className="two-col">
                        <div>
                          <label className="field-label" htmlFor="interview-position">Position</label>
                          <input
                            id="interview-position"
                            type="text"
                            value={interviewPosition}
                            onChange={(event) => setInterviewPosition(event.target.value)}
                            placeholder="e.g. AIML Intern"
                          />
                        </div>
                        <div>
                          <label className="field-label" htmlFor="interview-topic">Topic / domain</label>
                          <input
                            id="interview-topic"
                            type="text"
                            value={interviewTopic}
                            onChange={(event) => setInterviewTopic(event.target.value)}
                            placeholder="e.g. Machine Learning fundamentals"
                          />
                        </div>
                      </div>
                      <div>
                        <label className="field-label" htmlFor="interview-focus">Specific focus areas</label>
                        <input
                          id="interview-focus"
                          type="text"
                          value={interviewFocus}
                          onChange={(event) => setInterviewFocus(event.target.value)}
                          placeholder="e.g. Python, ML basics, projects, resume"
                        />
                      </div>
                      <div className="interview-toolbar">
                        <button type="button" className="btn btn-primary" onClick={startInterviewSession} disabled={chatLoading}>
                          {chatLoading ? 'Starting...' : 'Start'}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="interview-toolbar compact interview-resume-bar" onClick={resumeInterviewerHandsFree} role="button" tabIndex={0} onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          resumeInterviewerHandsFree();
                        }
                      }}>
                        <div className="interview-pill">
                          {interviewPosition || 'Interview'}
                        </div>
                        <div className="interview-pill subtle">
                          {interviewTopic || 'General topic'}
                        </div>
                      </div>

                      <div className="chat-thread chat-thread-page" ref={chatScrollRef}>
                        {interviewerMessages.map((item) => (
                          <div key={item.id} className={`chat-bubble-row ${item.role === 'user' ? 'user' : 'assistant'}`}>
                            <div
                              className={`chat-bubble-shell ${item.role === 'assistant' ? 'assistant' : 'user'}${
                                item.id === latestInterviewerAssistantId ? ' latest' : ''
                              }`}
                            >
                              <div className={`chat-bubble ${item.role === 'user' ? 'user' : 'assistant'}`}>{item.content}</div>
                              {item.role === 'assistant' ? (
                                <button
                                  type="button"
                                  className="chat-line-audio"
                                  onClick={() => speakInterviewerMessage(item.content)}
                                  aria-label="Play interviewer audio"
                                  title="Play interviewer audio"
                                >
                                  <svg viewBox="0 0 24 24" aria-hidden="true">
                                    <path
                                      d="M5 10v4h3l4 4V6L8 10H5zm10.5 2a3.5 3.5 0 0 0-1.74-3.03v6.06A3.5 3.5 0 0 0 15.5 12zm0-7.5v2.16a7 7 0 0 1 0 10.68v2.16a9 9 0 0 0 0-15z"
                                      fill="currentColor"
                                    />
                                  </svg>
                                </button>
                              ) : null}
                            </div>
                          </div>
                        ))}
                        {chatLoading ? (
                          <div className="chat-bubble-row assistant">
                            <div className="chat-bubble assistant chat-bubble-loading">Interviewer is thinking...</div>
                          </div>
                        ) : null}
                      </div>

                      {chatError ? <div className="small-error chat-error">{chatError}</div> : null}

                      <div className="chat-compose">
                        <div className="chat-compose-box">
                          <textarea
                            value={chatInput}
                            onChange={(event) => setChatInput(event.target.value)}
                            onKeyDown={handleChatInputKeyDown}
                            onMouseDown={handleInterviewAnswerFocus}
                            onFocus={handleInterviewAnswerFocus}
                            onBlur={handleInterviewAnswerBlur}
                            placeholder={interviewerListening ? 'Listening...' : 'Answer the interviewer here...'}
                          />
                          <div className="chat-compose-actions">
                            {speechRecognitionSupported ? (
                              <button
                                type="button"
                                className={`chat-compose-icon-btn ${interviewerListening ? 'active' : ''}`}
                                onClick={toggleInterviewListening}
                                aria-label={interviewerListening ? 'Stop microphone input' : 'Start microphone input'}
                                title={interviewerListening ? 'Stop microphone input' : 'Start microphone input'}
                              >
                                <svg viewBox="0 0 24 24" aria-hidden="true">
                                  <path
                                    d="M12 16a3 3 0 0 0 3-3V8a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V22h2v-2.08A7 7 0 0 0 19 13h-2z"
                                    fill="currentColor"
                                  />
                                </svg>
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="chat-compose-icon-btn chat-send-btn"
                              onClick={sendChatMessage}
                              disabled={chatLoading || !chatInput.trim()}
                              aria-label="Send interview answer"
                              title="Send interview answer"
                            >
                              <svg viewBox="0 0 24 24" aria-hidden="true">
                                <path
                                  d="M12 18V6M12 6l-5 5M12 6l5 5"
                                  fill="none"
                                  stroke="currentColor"
                                  strokeWidth="2.1"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                />
                              </svg>
                            </button>
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                </>
              )}
            </div>
          </section>
        )}

        {chatOpen && chatMinimized && (
          <div
            className="chat-dock floating"
            style={{ left: `${chatWindow.x}px`, top: `${chatWindow.y}px`, width: `${chatWindow.width}px`, height: `${chatWindow.height}px` }}
          >
            <div className="chat-panel chat-panel-floating">
              <div className="chat-panel-head chat-panel-head-draggable" onMouseDown={startDraggingChat}>
                <div>
                  <div className="chat-panel-title">EvalAI Chat</div>
                </div>
                <div className="chat-panel-actions">
                  {chatMode === 'interviewer' ? (
                    <button type="button" className="chat-dismiss-btn" onClick={dismissChatWorkspace}>
                      Dismiss
                    </button>
                  ) : (
                    <button type="button" className="chat-dismiss-btn" onClick={() => switchChatMode('interviewer')}>
                      Interviewer
                    </button>
                  )}
                  <button type="button" className="chat-head-btn" onClick={restoreChatWorkspace} title="Maximize chat" aria-label="Maximize chat">
                    ⛶
                  </button>
                </div>
              </div>

              <div className="chat-thread" ref={chatScrollRef}>
                {(chatMode === 'interviewer' ? interviewerMessages : autoChatMessages).map((item) => (
                  <div key={item.id} className={`chat-bubble-row ${item.role === 'user' ? 'user' : 'assistant'}`}>
                    <div
                      className={`chat-bubble-shell ${item.role === 'assistant' ? 'assistant' : 'user'}${
                        item.id === latestInterviewerAssistantId ? ' latest' : ''
                      }`}
                    >
                      <div className={`chat-bubble ${item.role === 'user' ? 'user' : 'assistant'}`}>{item.content}</div>
                      {chatMode === 'interviewer' && item.role === 'assistant' ? (
                        <button
                          type="button"
                          className="chat-line-audio"
                          onClick={() => speakInterviewerMessage(item.content)}
                          aria-label="Play interviewer audio"
                          title="Play interviewer audio"
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true">
                            <path
                              d="M5 10v4h3l4 4V6L8 10H5zm10.5 2a3.5 3.5 0 0 0-1.74-3.03v6.06A3.5 3.5 0 0 0 15.5 12zm0-7.5v2.16a7 7 0 0 1 0 10.68v2.16a9 9 0 0 0 0-15z"
                              fill="currentColor"
                            />
                          </svg>
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
                {chatLoading ? (
                  <div className="chat-bubble-row assistant">
                    <div className="chat-bubble assistant chat-bubble-loading">
                      {chatMode === 'interviewer' ? 'Interviewer is thinking...' : 'Thinking...'}
                    </div>
                  </div>
                ) : null}
              </div>

              {chatError ? <div className="small-error chat-error">{chatError}</div> : null}

              <div className="chat-compose">
                <div className="chat-compose-box">
                  <textarea
                    value={chatInput}
                    onChange={(event) => setChatInput(event.target.value)}
                    onKeyDown={handleChatInputKeyDown}
                    onMouseDown={chatMode === 'interviewer' ? handleInterviewAnswerFocus : undefined}
                    onFocus={chatMode === 'interviewer' ? handleInterviewAnswerFocus : undefined}
                    onBlur={chatMode === 'interviewer' ? handleInterviewAnswerBlur : undefined}
                    placeholder={chatMode === 'interviewer' ? (interviewerListening ? 'Listening...' : 'Answer the interviewer here...') : 'Ask anything...'}
                  />
                  <div className="chat-compose-actions">
                    {chatMode === 'interviewer' && speechRecognitionSupported ? (
                      <button
                        type="button"
                        className={`chat-compose-icon-btn ${interviewerListening ? 'active' : ''}`}
                        onClick={toggleInterviewListening}
                        aria-label={interviewerListening ? 'Stop microphone input' : 'Start microphone input'}
                        title={interviewerListening ? 'Stop microphone input' : 'Start microphone input'}
                      >
                        <svg viewBox="0 0 24 24" aria-hidden="true">
                          <path
                            d="M12 16a3 3 0 0 0 3-3V8a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V22h2v-2.08A7 7 0 0 0 19 13h-2z"
                            fill="currentColor"
                          />
                        </svg>
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="chat-compose-icon-btn chat-send-btn"
                      onClick={sendChatMessage}
                      disabled={chatLoading || !chatInput.trim()}
                      aria-label="Send message"
                      title="Send message"
                    >
                      <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path
                          d="M12 18V6M12 6l-5 5M12 6l5 5"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2.1"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
              <button type="button" className="chat-resize-handle" onMouseDown={startResizingChat} aria-label="Resize chat window" />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
