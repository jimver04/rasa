from __future__ import annotations
from pathlib import Path
from collections import defaultdict
from abc import abstractmethod
import jsonpickle
import logging

from tqdm import tqdm
from typing import Tuple, List, Optional, Dict, Text, Union, Any, Iterator, Set
import numpy as np


from rasa.core.featurizers.single_state_featurizer import SingleStateFeaturizer
from rasa.core.featurizers.precomputation import MessageContainerForCoreFeaturization
from rasa.core.exceptions import InvalidTrackerFeaturizerUsageError
import rasa.shared.core.trackers
import rasa.shared.utils.io
from rasa.shared.nlu.constants import TEXT, INTENT, ENTITIES, ACTION_NAME
from rasa.shared.nlu.training_data.features import Features
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.shared.core.domain import State, Domain
from rasa.shared.core.events import Event, ActionExecuted, UserUttered
from rasa.shared.core.constants import (
    USER,
    ACTION_UNLIKELY_INTENT_NAME,
    PREVIOUS_ACTION,
)
from rasa.shared.exceptions import RasaException
from rasa.utils.tensorflow.constants import LABEL_PAD_ID
from rasa.core.featurizers import _tracker_featurizers

FEATURIZER_FILE = "featurizer.json"

logger = logging.getLogger(__name__)

# All code outside this module will continue to use the old `tracker_featurizer` module
# TODO: This is a workaround around until we have all components migrated to
# `GraphComponent`.
TrackerFeaturizer = _tracker_featurizers.TrackerFeaturizer
MaxHistoryTrackerFeaturizer = _tracker_featurizers.MaxHistoryTrackerFeaturizer
IntentMaxHistoryTrackerFeaturizer = (
    _tracker_featurizers.IntentMaxHistoryTrackerFeaturizer
)
FullDialogueTrackerFeaturizer = _tracker_featurizers.FullDialogueTrackerFeaturizer


class InvalidStory(RasaException):
    """Exception that can be raised if story cannot be featurized."""

    def __init__(self, message: Text) -> None:
        """Creates an InvalidStory exception.

        Args:
          message: a custom exception message.
        """
        self.message = message
        super(InvalidStory, self).__init__()

    def __str__(self) -> Text:
        return self.message


class TrackerFeaturizer2:
    """Base class for actual tracker featurizers."""

    def __init__(
        self, state_featurizer: Optional[SingleStateFeaturizer] = None
    ) -> None:
        """Initializes the tracker featurizer.

        Args:
            state_featurizer: The state featurizer used to encode tracker states.
        """
        self.state_featurizer = state_featurizer

    @staticmethod
    def _create_states(
        tracker: DialogueStateTracker,
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
    ) -> List[State]:
        """Creates states for the given tracker.

        Args:
            tracker: The tracker to transform to states.
            domain: The domain of the tracker.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_rule_only_turns: If `True` ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.

        Returns:
            Trackers as states.
        """
        return tracker.past_states(
            domain,
            omit_unset_slots=omit_unset_slots,
            ignore_rule_only_turns=ignore_rule_only_turns,
            rule_only_data=rule_only_data,
        )

    def _featurize_states(
        self,
        trackers_as_states: List[List[State]],
        precomputations: Optional[MessageContainerForCoreFeaturization],
    ) -> List[List[Dict[Text, List[Features]]]]:
        """Featurizes state histories with `state_featurizer`.

        Args:
            trackers_as_states: Lists of states produced by a `DialogueStateTracker`
                instance.
            precomputations: Contains precomputed features and attributes.

        Returns:
            Featurized tracker states.
        """
        return [
            [
                self.state_featurizer.encode_state(state, precomputations)
                for state in tracker_states
            ]
            for tracker_states in trackers_as_states
        ]

    @staticmethod
    def _convert_labels_to_ids(
        trackers_as_actions: List[List[Text]], domain: Domain
    ) -> np.ndarray:
        """Converts actions to label ids for each tracker.

        Args:
            trackers_as_actions: A list of tracker labels.

        Returns:
            Label IDs for each tracker
        """
        # store labels in numpy arrays so that it corresponds to np arrays of input
        # features
        return np.array(
            [
                np.array(
                    [domain.index_for_action(action) for action in tracker_actions]
                )
                for tracker_actions in trackers_as_actions
            ]
        )

    def _create_entity_tags(
        self,
        trackers_as_entities: List[List[Dict[Text, Any]]],
        precomputations: Optional[MessageContainerForCoreFeaturization],
        bilou_tagging: bool = False,
    ) -> List[List[Dict[Text, List[Features]]]]:
        """Featurizes extracted entities with `state_featurizer`.

        Args:
            trackers_as_entities: Extracted entities from trackers.
            precomputations: Contains precomputed features and attributes.
            bilou_tagging: When `True` use the BILOU tagging scheme.

        Returns:
            Trackers as entity features.
        """
        return [
            [
                self.state_featurizer.encode_entities(
                    entity_data, precomputations, bilou_tagging,
                )
                for entity_data in trackers_entities
            ]
            for trackers_entities in trackers_as_entities
        ]

    @staticmethod
    def _entity_data(event: UserUttered) -> Dict[Text, Any]:
        """Extracts entities from event if not using intents.

        Args:
            event: The event from which to extract entities.

        Returns:
            Event text and entities if no intent is present.
        """
        # train stories support both text and intent,
        # but if intent is present, the text is ignored
        if event.text and not event.intent_name:
            return {TEXT: event.text, ENTITIES: event.entities}

        # input is not textual, so add empty dict
        return {}

    @staticmethod
    def _remove_user_text_if_intent(trackers_as_states: List[List[State]]) -> None:
        """Deletes user text from state dictionaries if intent is present.

        Only featurizing either the intent or user text is currently supported. When
        both are present in a state, the user text is removed so that only the intent
        is featurized.

        `trackers_as_states` is modified in place.

        Args:
            trackers_as_states: States produced by a `DialogueStateTracker` instance.
        """
        for states in trackers_as_states:
            for state in states:
                # remove text features to only use intent
                if state.get(USER, {}).get(INTENT) and state.get(USER, {}).get(TEXT):
                    del state[USER][TEXT]

    def training_states_actions_and_entities(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]], List[List[Dict[Text, Any]]]]:
        """Transforms trackers to states, actions, and entity data.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                 from training states.

        Returns:
            Trackers as states, actions, and entity data.
        """
        rasa.shared.utils.io.raise_deprecation_warning(
            "'training_states_actions_and_entities' is being deprecated in favor of "
            "'training_states_labels_and_entities'."
        )
        return self.training_states_labels_and_entities(
            trackers,
            domain,
            omit_unset_slots=omit_unset_slots,
            ignore_action_unlikely_intent=ignore_action_unlikely_intent,
        )

    def training_states_and_actions(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]]]:
        """Transforms trackers to states and actions.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states and actions.
        """
        rasa.shared.utils.io.raise_deprecation_warning(
            "'training_states_and_actions' is being deprecated in favor of "
            "'training_states_and_labels'."
        )

        return self.training_states_and_labels(
            trackers,
            domain,
            omit_unset_slots=omit_unset_slots,
            ignore_action_unlikely_intent=ignore_action_unlikely_intent,
        )

    def training_states_and_labels(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]]]:
        """Transforms trackers to states and labels.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states and labels.
        """
        (
            trackers_as_states,
            trackers_as_labels,
            _,
        ) = self.training_states_labels_and_entities(
            trackers,
            domain,
            omit_unset_slots=omit_unset_slots,
            ignore_action_unlikely_intent=ignore_action_unlikely_intent,
        )
        return trackers_as_states, trackers_as_labels

    @abstractmethod
    def training_states_labels_and_entities(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]], List[List[Dict[Text, Any]]]]:
        """Transforms trackers to states, labels, and entity data.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states, labels, and entity data.
        """
        raise NotImplementedError(
            f"`{self.__class__.__name__}` should implement how to "
            f"encode trackers as feature vectors"
        )

    def prepare_for_featurization(
        self, domain: Domain, bilou_tagging: bool = False,
    ) -> None:
        """Ensures that the featurizer is ready to be called during training.

        State featurizer needs to build its vocabulary from the domain
        for it to be ready to be used during training.

        Args:
            domain: Domain of the assistant.
            bilou_tagging: Whether to consider bilou tagging.
        """
        if self.state_featurizer is None:
            raise InvalidTrackerFeaturizerUsageError(
                f"Instance variable 'state_featurizer' is not set. "
                f"During initialization set 'state_featurizer' to an instance of "
                f"'{SingleStateFeaturizer.__class__.__name__}' class "
                f"to get numerical features for trackers."
            )
        self.state_featurizer.prepare_for_training(domain, bilou_tagging)

    def featurize_trackers(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        precomputations: Optional[MessageContainerForCoreFeaturization],
        bilou_tagging: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[
        List[List[Dict[Text, List[Features]]]],
        np.ndarray,
        List[List[Dict[Text, List[Features]]]],
    ]:
        """Featurizes the training trackers.

        Args:
            trackers: list of training trackers
            domain: the domain
            precomputations: Contains precomputed features and attributes.
            bilou_tagging: indicates whether BILOU tagging should be used or not
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training state features.

        Returns:
            - a dictionary of state types (INTENT, TEXT, ACTION_NAME, ACTION_TEXT,
              ENTITIES, SLOTS, ACTIVE_LOOP) to a list of features for all dialogue
              turns in all training trackers
            - the label ids (e.g. action ids) for every dialogue turn in all training
              trackers
            - A dictionary of entity type (ENTITY_TAGS) to a list of features
              containing entity tag ids for text user inputs otherwise empty dict
              for all dialogue turns in all training trackers
        """
        self.prepare_for_featurization(domain, bilou_tagging)
        (
            trackers_as_states,
            trackers_as_labels,
            trackers_as_entities,
        ) = self.training_states_labels_and_entities(
            trackers,
            domain,
            ignore_action_unlikely_intent=ignore_action_unlikely_intent,
        )

        tracker_state_features = self._featurize_states(
            trackers_as_states, precomputations
        )

        if not tracker_state_features and not trackers_as_labels:
            # If input and output were empty, it means there is
            # no data on which the policy can be trained
            # hence return them as it is. They'll be handled
            # appropriately inside the policy.
            return tracker_state_features, np.ndarray(trackers_as_labels), []

        label_ids = self._convert_labels_to_ids(trackers_as_labels, domain)

        entity_tags = self._create_entity_tags(
            trackers_as_entities, precomputations, bilou_tagging
        )

        return tracker_state_features, label_ids, entity_tags

    def _choose_last_user_input(
        self, trackers_as_states: List[List[State]], use_text_for_last_user_input: bool
    ) -> None:
        for states in trackers_as_states:
            last_state = states[-1]
            # only update the state of the real user utterance
            if not rasa.shared.core.trackers.is_prev_action_listen_in_state(last_state):
                continue

            if use_text_for_last_user_input:
                # remove intent features to only use text
                if last_state.get(USER, {}).get(INTENT):
                    del last_state[USER][INTENT]
                # don't add entities if text is used for featurization
                if last_state.get(USER, {}).get(ENTITIES):
                    del last_state[USER][ENTITIES]
            else:
                # remove text features to only use intent
                if last_state.get(USER, {}).get(TEXT):
                    del last_state[USER][TEXT]

        # make sure that all dialogue steps are either intent or text based
        self._remove_user_text_if_intent(trackers_as_states)

    def prediction_states(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        use_text_for_last_user_input: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        ignore_action_unlikely_intent: bool = False,
    ) -> List[List[State]]:
        """Transforms trackers to states for prediction.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            use_text_for_last_user_input: Indicates whether to use text or intent label
                for featurizing last user input.
            ignore_rule_only_turns: If True ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.
            ignore_action_unlikely_intent: Whether to remove states containing
                `action_unlikely_intent` from prediction states.

        Returns:
            Trackers as states for prediction.
        """
        raise NotImplementedError(
            "Featurizer must have the capacity to create feature vector"
        )

    def create_state_features(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        precomputations: Optional[MessageContainerForCoreFeaturization],
        use_text_for_last_user_input: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        ignore_action_unlikely_intent: bool = False,
    ) -> List[List[Dict[Text, List[Features]]]]:
        """Creates state features for prediction.

        Args:
            trackers: A list of state trackers
            domain: The domain
            precomputations: Contains precomputed features and attributes.
            use_text_for_last_user_input: Indicates whether to use text or intent label
                for featurizing last user input.
            ignore_rule_only_turns: If True ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.
            ignore_action_unlikely_intent: Whether to remove any states containing
                `action_unlikely_intent` from state features.

        Returns:
            Dictionaries of state type (INTENT, TEXT, ACTION_NAME, ACTION_TEXT,
            ENTITIES, SLOTS, ACTIVE_LOOP) to a list of features for all dialogue
            turns in all trackers.
        """
        trackers_as_states = self.prediction_states(
            trackers,
            domain,
            use_text_for_last_user_input,
            ignore_rule_only_turns,
            rule_only_data,
            ignore_action_unlikely_intent=ignore_action_unlikely_intent,
        )
        return self._featurize_states(trackers_as_states, precomputations)

    def persist(self, path: Union[Text, Path]) -> None:
        """Persists the tracker featurizer to the given path.

        Args:
            path: The path to persist the tracker featurizer to.
        """
        featurizer_file = Path(path) / FEATURIZER_FILE
        rasa.shared.utils.io.create_directory_for_file(featurizer_file)

        # entity tags are persisted in TED policy, they are not needed for prediction
        if self.state_featurizer is not None:
            self.state_featurizer.entity_tag_specs = None

        # noinspection PyTypeChecker
        rasa.shared.utils.io.write_text_file(
            str(jsonpickle.encode(self)), featurizer_file
        )

    @staticmethod
    def load(path: Union[Text, Path]) -> Optional[TrackerFeaturizer2]:
        """Loads the featurizer from file.

        Args:
            path: The path to load the tracker featurizer from.

        Returns:
            The loaded tracker featurizer.
        """
        featurizer_file = Path(path) / FEATURIZER_FILE
        if featurizer_file.is_file():
            return jsonpickle.decode(rasa.shared.utils.io.read_file(featurizer_file))

        logger.error(
            f"Couldn't load featurizer for policy. "
            f"File '{featurizer_file}' doesn't exist."
        )
        return None

    @staticmethod
    def _remove_action_unlikely_intent_from_states(states: List[State]) -> List[State]:
        return [
            state
            for state in states
            if not _is_prev_action_unlikely_intent_in_state(state)
        ]

    @staticmethod
    def _remove_action_unlikely_intent_from_events(events: List[Event]) -> List[Event]:
        return [
            event
            for event in events
            if (
                not isinstance(event, ActionExecuted)
                or event.action_name != ACTION_UNLIKELY_INTENT_NAME
            )
        ]


class FullDialogueTrackerFeaturizer2(TrackerFeaturizer2):
    """Creates full dialogue training data for time distributed architectures.

    Creates training data that uses each time output for prediction.
    """

    def training_states_labels_and_entities(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[
        List[List[State]], List[List[Optional[Text]]], List[List[Dict[Text, Any]]]
    ]:
        """Transforms trackers to states, action labels, and entity data.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states, action labels, and entity data.
        """
        trackers_as_states = []
        trackers_as_actions = []
        trackers_as_entities = []

        logger.debug(
            "Creating states and action examples from "
            "collected trackers (by {}({}))..."
            "".format(type(self).__name__, type(self.state_featurizer).__name__)
        )
        pbar = tqdm(
            trackers,
            desc="Processed trackers",
            disable=rasa.shared.utils.io.is_logging_disabled(),
        )
        for tracker in pbar:
            states = self._create_states(
                tracker, domain, omit_unset_slots=omit_unset_slots
            )
            events = tracker.applied_events()

            if ignore_action_unlikely_intent:
                states = self._remove_action_unlikely_intent_from_states(states)
                events = self._remove_action_unlikely_intent_from_events(events)

            delete_first_state = False
            actions = []
            entities = []
            entity_data = {}
            for event in events:
                if isinstance(event, UserUttered):
                    entity_data = self._entity_data(event)

                if not isinstance(event, ActionExecuted):
                    continue

                if not event.unpredictable:
                    # only actions which can be
                    # predicted at a stories start
                    actions.append(event.action_name or event.action_text)
                    entities.append(entity_data)
                else:
                    # unpredictable actions can be
                    # only the first in the story
                    if delete_first_state:
                        raise InvalidStory(
                            f"Found two unpredictable actions in one story "
                            f"'{tracker.sender_id}'. Check your story files."
                        )
                    delete_first_state = True

                # reset entity_data for the the next turn
                entity_data = {}

            if delete_first_state:
                states = states[1:]

            trackers_as_states.append(states[:-1])
            trackers_as_actions.append(actions)
            trackers_as_entities.append(entities)

        self._remove_user_text_if_intent(trackers_as_states)

        return trackers_as_states, trackers_as_actions, trackers_as_entities

    def prediction_states(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        use_text_for_last_user_input: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        ignore_action_unlikely_intent: bool = False,
    ) -> List[List[State]]:
        """Transforms trackers to states for prediction.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            use_text_for_last_user_input: Indicates whether to use text or intent label
                for featurizing last user input.
            ignore_rule_only_turns: If True ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.
            ignore_action_unlikely_intent: Whether to remove any states containing
                `action_unlikely_intent` from prediction states.

        Returns:
            Trackers as states for prediction.
        """
        trackers_as_states = [
            self._create_states(
                tracker,
                domain,
                ignore_rule_only_turns=ignore_rule_only_turns,
                rule_only_data=rule_only_data,
            )
            for tracker in trackers
        ]

        if ignore_action_unlikely_intent:
            trackers_as_states = [
                self._remove_action_unlikely_intent_from_states(states)
                for states in trackers_as_states
            ]

        self._choose_last_user_input(trackers_as_states, use_text_for_last_user_input)

        return trackers_as_states


class MaxHistoryTrackerFeaturizer2(TrackerFeaturizer2):
    """Truncates the tracker history into `max_history` long sequences.

    Creates training data from trackers where actions are the output prediction
    labels. Tracker state sequences which represent policy input are truncated
    to not excede `max_history` states.
    """

    LABEL_NAME = "action"

    def __init__(
        self,
        state_featurizer: Optional[SingleStateFeaturizer] = None,
        max_history: Optional[int] = None,
        remove_duplicates: bool = True,
    ) -> None:
        """Initializes the tracker featurizer.

        Args:
            state_featurizer: The state featurizer used to encode the states.
            max_history: The maximum length of an extracted state sequence.
            remove_duplicates: Keep only unique training state sequence/label pairs.
        """
        super().__init__(state_featurizer)
        self.max_history = max_history
        self.remove_duplicates = remove_duplicates

    @staticmethod
    def slice_state_history(
        states: List[State], slice_length: Optional[int]
    ) -> List[State]:
        """Slices states from the trackers history.

        Args:
            states: The states
            slice_length: The slice length

        Returns:
            The sliced states.
        """
        if not slice_length:
            return states

        return states[-slice_length:]

    @staticmethod
    def _hash_example(states: List[State], labels: Optional[List[Text]] = None) -> int:
        """Hashes states (and optionally label).

        Produces a hash of the tracker state sequence (and optionally the labels).
        If `labels` is `None`, labels don't get hashed.

        Args:
            states: The tracker state sequence to hash.
            labels: Label strings associated with this state sequence.

        Returns:
            The hash of the states and (optionally) the label.
        """
        frozen_states = tuple(
            s if s is None else DialogueStateTracker.freeze_current_state(s)
            for s in states
        )
        if labels is not None:
            frozen_labels = tuple(labels)
            return hash((frozen_states, frozen_labels))
        else:
            return hash(frozen_states)

    def training_states_labels_and_entities(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]], List[List[Dict[Text, Any]]]]:
        """Transforms trackers to states, action labels, and entity data.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states, labels, and entity data.
        """
        example_states = []
        example_labels = []
        example_entities = []

        # Store of example hashes for removing duplicate training examples.
        hashed_examples = set()

        logger.debug(
            f"Creating states and {self.LABEL_NAME} label examples from "
            f"collected trackers "
            f"(by {type(self).__name__}({type(self.state_featurizer).__name__}))..."
        )
        pbar = tqdm(
            trackers,
            desc="Processed trackers",
            disable=rasa.shared.utils.io.is_logging_disabled(),
        )
        for tracker in pbar:

            for states, label, entities in self._extract_examples(
                tracker,
                domain,
                omit_unset_slots=omit_unset_slots,
                ignore_action_unlikely_intent=ignore_action_unlikely_intent,
            ):

                if self.remove_duplicates:
                    hashed = self._hash_example(states, label)
                    if hashed in hashed_examples:
                        continue
                    hashed_examples.add(hashed)

                example_states.append(states)
                example_labels.append(label)
                example_entities.append(entities)

                pbar.set_postfix({f"# {self.LABEL_NAME}": f"{len(example_labels):d}"})

        self._remove_user_text_if_intent(example_states)

        logger.debug(f"Created {len(example_states)} {self.LABEL_NAME} examples.")

        return example_states, example_labels, example_entities

    def _extract_examples(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Iterator[Tuple[List[State], List[Text], List[Dict[Text, Any]]]]:
        """Creates an iterator over training examples from a tracker.

        Args:
            trackers: The tracker from which to extract training examples.
            domain: The domain of the training data.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            An iterator over example states, labels, and entity data.
        """
        tracker_states = self._create_states(
            tracker, domain, omit_unset_slots=omit_unset_slots
        )
        events = tracker.applied_events()

        if ignore_action_unlikely_intent:
            tracker_states = self._remove_action_unlikely_intent_from_states(
                tracker_states
            )
            events = self._remove_action_unlikely_intent_from_events(events)

        label_index = 0
        entity_data = {}
        for event in events:
            if isinstance(event, UserUttered):
                entity_data = self._entity_data(event)

            elif isinstance(event, ActionExecuted):

                label_index += 1

                # use only actions which can be predicted at a stories start
                if event.unpredictable:
                    continue

                sliced_states = self.slice_state_history(
                    tracker_states[:label_index], self.max_history
                )
                label = [event.action_name or event.action_text]
                entities = [entity_data]

                yield sliced_states, label, entities

                # reset entity_data for the the next turn
                entity_data = {}

    def prediction_states(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        use_text_for_last_user_input: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        ignore_action_unlikely_intent: bool = False,
    ) -> List[List[State]]:
        """Transforms trackers to states for prediction.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            use_text_for_last_user_input: Indicates whether to use text or intent label
                for featurizing last user input.
            ignore_rule_only_turns: If True ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.
            ignore_action_unlikely_intent: Whether to remove any states containing
                `action_unlikely_intent` from prediction states.

        Returns:
            Trackers as states for prediction.
        """
        trackers_as_states = [
            self._create_states(
                tracker,
                domain,
                ignore_rule_only_turns=ignore_rule_only_turns,
                rule_only_data=rule_only_data,
            )
            for tracker in trackers
        ]

        # Remove `action_unlikely_intent` from `trackers_as_states`.
        # This must be done before state history slicing to ensure the
        # max history of the sliced states matches training time.
        if ignore_action_unlikely_intent:
            trackers_as_states = [
                self._remove_action_unlikely_intent_from_states(states)
                for states in trackers_as_states
            ]

        trackers_as_states = [
            self.slice_state_history(states, self.max_history)
            for states in trackers_as_states
        ]
        self._choose_last_user_input(trackers_as_states, use_text_for_last_user_input)

        return trackers_as_states


class IntentMaxHistoryTrackerFeaturizer2(MaxHistoryTrackerFeaturizer2):
    """Truncates the tracker history into `max_history` long sequences.

    Creates training data from trackers where intents are the output prediction
    labels. Tracker state sequences which represent policy input are truncated
    to not excede `max_history` states.
    """

    LABEL_NAME = "intent"

    @classmethod
    def _convert_labels_to_ids(
        cls, trackers_as_intents: List[List[Text]], domain: Domain
    ) -> np.ndarray:
        """Converts a list of labels to a matrix of label ids.

        The number of rows is equal to `len(trackers_as_intents)`. The number of
        columns is equal to the maximum number of positive labels that any training
        example is associated with. Rows are padded with `LABEL_PAD_ID` if not all rows
        have the same number of labels.

        Args:
            trackers_as_intents: Positive example label ids
                associated with each training example.
            domain: The domain of the training data.

        Returns:
           A matrix of label ids.
        """
        # store labels in numpy arrays so that it corresponds to np arrays
        # of input features
        label_ids = [
            [domain.intents.index(intent) for intent in tracker_intents]
            for tracker_intents in trackers_as_intents
        ]

        return np.array(cls._pad_label_ids(label_ids))

    @staticmethod
    def _pad_label_ids(label_ids: List[List[int]]) -> List[List[int]]:
        """Pads label ids so that all are of the same length.

        Args:
            label_ids: Label ids of varying lengths

        Returns:
            Label ids padded to be of uniform length.
        """
        # If `label_ids` is an empty list, no padding needs to be added.
        if not label_ids:
            return label_ids

        # Add `LABEL_PAD_ID` padding to labels array so that
        # each example has equal number of labels
        multiple_labels_count = [len(a) for a in label_ids]
        max_labels_count = max(multiple_labels_count)
        num_padding_needed = [max_labels_count - len(a) for a in label_ids]

        padded_label_ids = []
        for ids, num_pads in zip(label_ids, num_padding_needed):
            padded_row = list(ids) + [LABEL_PAD_ID] * num_pads
            padded_label_ids.append(padded_row)
        return padded_label_ids

    def training_states_labels_and_entities(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Tuple[List[List[State]], List[List[Text]], List[List[Dict[Text, Any]]]]:
        """Transforms trackers to states, intent labels, and entity data.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            Trackers as states, labels, and entity data.
        """
        example_states = []
        example_entities = []

        # Store of example hashes (of both states and labels) for removing
        # duplicate training examples.
        hashed_examples = set()
        # Mapping of example state hash to set of
        # positive labels associated with the state.
        state_hash_to_label_set: defaultdict[int, Set[Text]] = defaultdict(set)

        logger.debug(
            f"Creating states and {self.LABEL_NAME} label examples from "
            f"collected trackers "
            f"(by {type(self).__name__}({type(self.state_featurizer).__name__}))..."
        )
        pbar = tqdm(
            trackers,
            desc="Processed trackers",
            disable=rasa.shared.utils.io.is_logging_disabled(),
        )
        for tracker in pbar:

            for states, label, entities in self._extract_examples(
                tracker,
                domain,
                omit_unset_slots=omit_unset_slots,
                ignore_action_unlikely_intent=ignore_action_unlikely_intent,
            ):

                if self.remove_duplicates:
                    hashed = self._hash_example(states, label)
                    if hashed in hashed_examples:
                        continue
                    hashed_examples.add(hashed)

                # Store all positive labels associated with a training state.
                state_hash = self._hash_example(states)

                # Only add unique example states unless `remove_duplicates` is `False`.
                if (
                    not self.remove_duplicates
                    or state_hash not in state_hash_to_label_set
                ):
                    example_states.append(states)
                    example_entities.append(entities)

                state_hash_to_label_set[state_hash].add(label[0])

                pbar.set_postfix({f"# {self.LABEL_NAME}": f"{len(example_states):d}"})

        # Collect positive labels for each state example.
        example_labels = [
            list(state_hash_to_label_set[self._hash_example(state)])
            for state in example_states
        ]

        self._remove_user_text_if_intent(example_states)

        logger.debug(f"Created {len(example_states)} {self.LABEL_NAME} examples.")

        return example_states, example_labels, example_entities

    def _extract_examples(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        omit_unset_slots: bool = False,
        ignore_action_unlikely_intent: bool = False,
    ) -> Iterator[Tuple[List[State], List[Text], List[Dict[Text, Any]]]]:
        """Creates an iterator over training examples from a tracker.

        Args:
            tracker: The tracker from which to extract training examples.
            domain: The domain of the training data.
            omit_unset_slots: If `True` do not include the initial values of slots.
            ignore_action_unlikely_intent: Whether to remove `action_unlikely_intent`
                from training states.

        Returns:
            An iterator over example states, labels, and entity data.
        """
        tracker_states = self._create_states(
            tracker, domain, omit_unset_slots=omit_unset_slots
        )
        events = tracker.applied_events()

        if ignore_action_unlikely_intent:
            tracker_states = self._remove_action_unlikely_intent_from_states(
                tracker_states
            )
            events = self._remove_action_unlikely_intent_from_events(events)

        label_index = 0
        for event in events:

            if isinstance(event, ActionExecuted):
                label_index += 1

            elif isinstance(event, UserUttered):

                sliced_states = self.slice_state_history(
                    tracker_states[:label_index], self.max_history
                )
                label = [event.intent_name or event.text]
                entities = [{}]

                yield sliced_states, label, entities

    @staticmethod
    def _cleanup_last_user_state_with_action_listen(
        trackers_as_states: List[List[State]],
    ) -> List[List[State]]:
        """Removes the last tracker state if the previous action is `action_listen`.

        States with the previous action equal to `action_listen` correspond to states
        with a new user intent. This information is what `UnexpecTEDIntentPolicy` is
        trying to predict so it needs to be removed before obtaining a prediction.

        Args:
            trackers_as_states: Trackers converted to states

        Returns:
            Filtered states with last `action_listen` removed.
        """
        for states in trackers_as_states:
            if not states:
                continue
            last_state = states[-1]
            if rasa.shared.core.trackers.is_prev_action_listen_in_state(last_state):
                del states[-1]

        return trackers_as_states

    def prediction_states(
        self,
        trackers: List[DialogueStateTracker],
        domain: Domain,
        use_text_for_last_user_input: bool = False,
        ignore_rule_only_turns: bool = False,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        ignore_action_unlikely_intent: bool = False,
    ) -> List[List[State]]:
        """Transforms trackers to states for prediction.

        Args:
            trackers: The trackers to transform.
            domain: The domain.
            use_text_for_last_user_input: Indicates whether to use text or intent label
                for featurizing last user input.
            ignore_rule_only_turns: If True ignore dialogue turns that are present
                only in rules.
            rule_only_data: Slots and loops,
                which only occur in rules but not in stories.
            ignore_action_unlikely_intent: Whether to remove any states containing
                `action_unlikely_intent` from prediction states.

        Returns:
            Trackers as states for prediction.
        """
        trackers_as_states = [
            self._create_states(
                tracker,
                domain,
                ignore_rule_only_turns=ignore_rule_only_turns,
                rule_only_data=rule_only_data,
            )
            for tracker in trackers
        ]

        # Remove `action_unlikely_intent` from `trackers_as_states`.
        # This must be done before state history slicing to ensure the
        # max history of the sliced states matches training time.
        if ignore_action_unlikely_intent:
            trackers_as_states = [
                self._remove_action_unlikely_intent_from_states(states)
                for states in trackers_as_states
            ]

        self._choose_last_user_input(trackers_as_states, use_text_for_last_user_input)

        # `tracker_as_states` contain a state with intent = last intent
        # and previous action = action_listen. This state needs to be
        # removed as it was not present during training as well because
        # predicting the last intent is what the policies using this
        # featurizer do. This is specifically done before state history
        # is sliced so that the number of past states is same as `max_history`.
        self._cleanup_last_user_state_with_action_listen(trackers_as_states)

        trackers_as_states = [
            self.slice_state_history(states, self.max_history)
            for states in trackers_as_states
        ]

        return trackers_as_states


def _is_prev_action_unlikely_intent_in_state(state: State) -> bool:
    prev_action_name = state.get(PREVIOUS_ACTION, {}).get(ACTION_NAME)
    return prev_action_name == ACTION_UNLIKELY_INTENT_NAME
