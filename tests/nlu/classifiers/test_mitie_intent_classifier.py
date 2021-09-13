import pytest

from rasa.engine.graph import ExecutionContext
from rasa.engine.storage.resource import Resource
from rasa.engine.storage.storage import ModelStorage
from rasa.nlu.classifiers.mitie_intent_classifier import (
    MitieIntentClassifierGraphComponent,
)
from rasa.nlu.tokenizers.mitie_tokenizer import MitieTokenizerGraphComponent

from rasa.nlu.utils.mitie_utils import MitieModel, MitieNLPGraphComponent
import rasa.shared.nlu.training_data.loading
from rasa.shared.nlu.constants import (
    TEXT,
    INTENT,
    INTENT_NAME_KEY,
    PREDICTED_CONFIDENCE_KEY,
)
from rasa.shared.nlu.training_data.message import Message


def create_tokenizer():
    return MitieTokenizerGraphComponent(
        MitieTokenizerGraphComponent.get_default_config()
    )


@pytest.fixture
def mitie_model(
    default_model_storage: ModelStorage, default_execution_context: ExecutionContext
) -> MitieModel:
    component = MitieNLPGraphComponent.create(
        MitieNLPGraphComponent.get_default_config(),
        default_model_storage,
        Resource("mitie"),
        default_execution_context,
    )

    return component.provide()


@pytest.mark.timeout(120)
def test_train_load_predict_loop(
    default_model_storage: ModelStorage,
    default_execution_context: ExecutionContext,
    mitie_model: MitieModel,
):
    resource = Resource("mitie_classifier")
    component = MitieIntentClassifierGraphComponent.create(
        MitieIntentClassifierGraphComponent.get_default_config(),
        default_model_storage,
        resource,
        default_execution_context,
    )

    training_data = rasa.shared.nlu.training_data.loading.load_data(
        "data/examples/rasa/demo-rasa.yml"
    )
    tokenizer = create_tokenizer()
    # Tokenize message as classifier needs that
    tokenizer.process_training_data(training_data)

    component.train(training_data, mitie_model)

    component = MitieIntentClassifierGraphComponent.load(
        MitieIntentClassifierGraphComponent.get_default_config(),
        default_model_storage,
        resource,
        default_execution_context,
    )

    test_message = Message({TEXT: "hi"})
    tokenizer.process([test_message])
    component.process([test_message], mitie_model)

    assert test_message.data[INTENT][INTENT_NAME_KEY] == "greet"
    assert test_message.data[INTENT][PREDICTED_CONFIDENCE_KEY] > 0


def test_load_from_untrained(
    default_model_storage: ModelStorage,
    default_execution_context: ExecutionContext,
    mitie_model: MitieModel,
):
    resource = Resource("some_resource")

    component = MitieIntentClassifierGraphComponent.load(
        MitieIntentClassifierGraphComponent.get_default_config(),
        default_model_storage,
        resource,
        default_execution_context,
    )

    test_message = Message({TEXT: "hi"})
    create_tokenizer().process([test_message])
    component.process([test_message], mitie_model)

    assert test_message.data[INTENT] == {"name": None, "confidence": 0.0}


def test_load_from_untrained_but_with_resource_existing(
    default_model_storage: ModelStorage,
    default_execution_context: ExecutionContext,
    mitie_model: MitieModel,
):
    resource = Resource("some_resource")

    with default_model_storage.write_to(resource):
        # This makes sure the directory exists but the model file itself doesn't
        pass

    component = MitieIntentClassifierGraphComponent.load(
        MitieIntentClassifierGraphComponent.get_default_config(),
        default_model_storage,
        resource,
        default_execution_context,
    )

    test_message = Message({TEXT: "hi"})
    create_tokenizer().process([test_message])
    component.process([test_message], mitie_model)

    assert test_message.data[INTENT] == {"name": None, "confidence": 0.0}
