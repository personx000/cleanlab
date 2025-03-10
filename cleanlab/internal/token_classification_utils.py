import string
import numpy as np
from termcolor import colored
from typing import Union, Optional, Callable, Tuple


def get_sentence(words: list) -> str:
    """
    Get sentence formed by a list of words with minor processing for readability

    Parameters
    ----------
    words: list
        list of word-level tokens

    Returns
    ----------
    sentences: string
        sentence formed by list of word-level tokens

    """
    sentence = ""
    for word in words:
        if word not in string.punctuation or word in ["-", "("]:
            word = " " + word
        sentence += word
    sentence = sentence.replace(" '", "'").replace("( ", "(").strip()
    return sentence


def filter_sentence(
    sentences: list, condition: Optional[Callable] = None, return_mask: bool = True
) -> Union[Tuple[list, list], list]:
    """
    Filter sentence based on some condition, and returns filter mask

    Parameters
    ----------
        sentences: list
            list of sentences

        condition: Callable
            sentence filtering condition

        return_mask: bool
            if set to True, also returns mask

    Returns
    ---------
        sentences: list
            list of sentences filtered

        mask: if `return_mask`, also returns a mask such that `mask[i] == True` if the i'th sentence is included in the
        filtered sentence, otherwise `mask[i] == False`

    """
    if not condition:
        condition = lambda sentence: len(sentence) > 1 and "#" not in sentence
    mask = list(map(condition, sentences))
    sentences = [sentence for m, sentence in zip(mask, sentences) if m]
    if return_mask:
        return sentences, mask
    else:
        return sentences


def process_token(token: str, replace: list = [("#", "")]) -> str:
    """
    Replaces special characters in the tokens

    Parameters
    ----------
        token: str
            token which potentially contains special characters

        replace: list
            list of tuples `(s1, s2)`, where all occurances of s1 are replaced by s2

    Returns
    ---------
        token: str
            processed token whose special character has been replaced
    """
    for old, new in replace:
        token = token.replace(old, new)
    return token


def mapping(entities: list, maps: list) -> list:
    """
    Map a list of entities to its corresponding entities

    Parameters
    ----------
        entities: list
            a list of given entities

        maps:
            a list of mapped entities, such that the i'th indexed token should be mapped to `maps[i]`

    Returns
    ---------
        mapped_entities:
            a list of mapped entities

    """
    f = lambda x: maps[x]
    return list(map(f, entities))


def merge_probs(probs: np.ndarray, maps: list) -> np.ndarray:
    """
    Merges model-predictive probabilities with desied mapping

    Parameters
    ----------
        probs: np.array
            np.array of shape `(N, K)`, where N is the number of tokens, and K is the number of classes for the model

        maps: list
            a list of mapped index, such that the probability of the token being in the i'th class is mapped to the
            `maps[i]` index. If `maps[i] == -1`, the i'th column of `probs` is ignored. If `np.any(maps == -1)`, the
            returned probability is re-normalized.

    Returns
    ---------
        probs_merged: np.array
            np.array of shape `(N, K')`, where K' is the number of new classes. Probablities are merged and
            re-normalized if necessary.

    """
    old_classes = probs.shape[1]
    probs_merged = np.zeros([len(probs), np.max(maps) + 1])

    for i in range(old_classes):
        if maps[i] >= 0:
            probs_merged[:, maps[i]] += probs[:, i]
    if -1 in maps:
        row_sums = probs_merged.sum(axis=1)
        probs_merged /= row_sums[:, np.newaxis]
    return probs_merged


def color_sentence(sentence: str, word: str) -> str:
    """
    Searches for a given token in the sentence and returns the sentence where the given token is colored red

    Parameters
    ----------
        sentence: str
            a sentence where the word is searched

        word: str
            keyword to find in `sentence`. Assumes the word exists in token

    Returns
    ---------
        colored_sentence: str
            `sentence` where the first occurance of the word is colored red, using `termcolor.colored`

    """
    start_idx = sentence.index(word)
    before, after = sentence[:start_idx], sentence[start_idx + len(word) :]
    return "%s%s%s" % (before, colored(word, "red"), after)
