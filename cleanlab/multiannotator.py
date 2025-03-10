import numpy as np
import pandas as pd
from typing import List, Dict, Any, Union, Tuple, Optional
from cleanlab.rank import get_label_quality_scores
from cleanlab.internal.util import get_num_classes
import warnings


def get_label_quality_multiannotator(
    labels_multiannotator: Union[pd.DataFrame, np.ndarray],
    pred_probs: np.ndarray,
    *,
    consensus_method: Union[str, List[str]] = "best_quality",
    quality_method: str = "crowdlab",
    return_detailed_quality: bool = True,
    return_annotator_stats: bool = True,
    verbose: bool = True,
    label_quality_score_kwargs: dict = {},
) -> Dict[str, pd.DataFrame]:
    """Returns label quality scores for each example and for each annotator.
    This function is for multiclass classification datasets where examples have been labeled by
    multiple annotators (not necessarily the same number of annotators per example).
    It computes one consensus label for each example that best accounts for the labels chosen by each
    annotator (and their quality), as well as a score for how confident we are that this consensus label is actually correct.
    It also computes the the quality scores for each annotator's individual labels, and the quality of each annotator.
    The score is between 0 and 1; lower scores
    indicate labels less likely to be correct. For example:
    1 - clean label (the given label is likely correct).
    0 - dirty label (the given label is unlikely correct).

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame of np.ndarray
        2D pandas DataFrame or array of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        labels_multiannotator[n][m] = given label for n-th example by m-th annotator.
        For a dataset with K classes, each given label must be an integer in 0, 1, ..., K-1 or
        NaN if this annotator did not label a particular example. Column names should correspond to the annotators' ID.
    pred_probs : np.ndarray
        An array of shape ``(N, K)`` of model-predicted probabilities.
        Predicted probabilities in the same format expected by the :py:func:`get_label_quality_scores <cleanlab.rank.get_label_quality_scores>`.
    consensus_method : str or List[str], default = "majority_vote"
        Specifies the method used to aggregate labels from multiple annotators into a single consensus label.
        Options include:
        - ``majority_vote``: consensus obtained using a simple majority vote among annotators, with ties broken via ``pred_probs``.
        - ``best_quality``: consensus obtained by selecting the label with highest label quality (quality determined by method specified in ``quality_method``)
        A List may be passed if you want to consider multiple methods for producing consensus labels.
        If a List is passed, then the 0th element of List is the method used to produce columns "consensus_label", "consensus_quality_score", "annotator_agreement" in the returned DataFrame.
        The 1st, 2nd, 3rd, etc. elements of this List are output as extra columns in the returned ``pandas DataFrame`` with names formatted as:
        consensus_label_SUFFIX, consensus_quality_score_SUFFIX
        where SUFFIX = the str element of this list, which must correspond to a valid method for computing consensus labels.
    quality_method : str, default = "crowdlab"
        Specifies the method used to calculate the quality of the consensus label.
        Options include:
        - ``crowdlab``: an emsemble method that weighs both the annotators' labels as well as the model's prediction
        - ``agreement``: the fraction of annotators that agree with the consensus label
    return_detailed_quality: bool, default = True
        Boolean to specify if `detailed_label_quality` is returned.
    return_annotator_stats : bool, default = True
        Boolean to specify if `annotator_stats` is returned.
    verbose : bool, default = True
        Certain warnings and notes will be printed if ``verbose`` is set to ``True``.
    label_quality_score_kwargs : dict, optional
        Keyword arguments to pass into ``get_label_quality_scores()``.

    Returns
    -------
    dict

    dictionary containing up to 3 pandas DataFrame with keys as below:
        ``label_quality`` : pandas.DataFrame
            pandas DataFrame in which each row corresponds to one example, with columns:
            - ``num_annotations``: the number of annotators that have labeled each example.
            - ``consensus_label``: the single label that is best for each example (you can control how it is derived from all annotators' labels via the argument: ``consensus_method``)
            - ``annotator_agreement``: the fraction of annotators that agree with the consensus label (only consider the annotators that labeled that particular example).
            - ``consensus_quality_score``: label quality score for consensus label, calculated using weighted product of `label_quality_score` of `consensus_label` and `annotator_agreement`

        ``detailed_label_quality`` : pandas.DataFrame (returned if `return_detailed_quality=True`)
            Returns a pandas DataFrame with columns ``quality_annotator_1``, ``quality_annotator_2``, ..., ``quality_annotator_M`` where each entry is
            the label quality score for the labels provided by each annotator (is ``NaN`` for examples which this annotator did not label).

        ``annotator_stats`` : pandas.DataFrame (returned if `return_annotator_stats=True`)
            Returns overall statistics about each annotator, sorted by lowest annotator_quality first.
            pandas DataFrame in which each row corresponds to one annotator (the row IDs correspond to annotator IDs), with columns:
            - ``annotator_quality``: overall quality of a given annotator's labels
            - ``num_examples_labeled``: number of examples annotated by a given annotator
            - ``agreement_with_consensus``: fraction of examples where a given annotator agrees with the consensus label
            - ``worst_class``: the class that is most frequently mislabeled by a given annotator
    """

    if isinstance(labels_multiannotator, np.ndarray):
        labels_multiannotator = pd.DataFrame(labels_multiannotator)

    # Raise error if labels_multiannotator has NaN rows
    if labels_multiannotator.isna().all(axis=1).any():
        raise ValueError("labels_multiannotator cannot have rows with all NaN.")

    # Raise error if labels_multiannotator has NaN columns
    if labels_multiannotator.isna().all().any():
        nan_columns = list(
            labels_multiannotator.columns[labels_multiannotator.isna().all() == True]
        )
        raise ValueError(
            f"""labels_multiannotator cannot have columns with all NaN.
            Annotators {nan_columns} did not label any examples."""
        )

    # Raise error if labels_multiannotator has <= 1 column
    if len(labels_multiannotator.columns) <= 1:
        raise ValueError(
            """labels_multiannotator must have more than one column. 
            If there is only one annotator, use cleanlab.rank.get_label_quality_scores instead"""
        )

    # Raise error if labels_multiannotator only has 1 label per example
    if labels_multiannotator.apply(lambda s: len(s.dropna()) == 1, axis=1).all():
        raise ValueError(
            """Each example only has one label, collapse the labels into a 1-D array and use
            cleanlab.rank.get_label_quality_scores instead"""
        )

    # Raise warning if no examples with 2 or more annotators agree
    # TODO: might shift this later in the code to avoid extra compute
    if labels_multiannotator.apply(
        lambda s: np.array_equal(s.dropna().unique(), s.dropna()), axis=1
    ).all():
        warnings.warn("Annotators do not agree on any example. Check input data.")

    # Count number of non-NaN values for each example
    num_annotations = labels_multiannotator.count(axis=1).to_numpy()

    if not isinstance(consensus_method, list):
        consensus_method = [consensus_method]

    if "best_quality" in consensus_method or "majority_vote" in consensus_method:
        majority_vote_label = get_majority_vote_label(
            labels_multiannotator=labels_multiannotator,
            pred_probs=pred_probs,
        )
        (
            MV_annotator_agreement,
            MV_consensus_quality_score,
            MV_post_pred_probs,
            MV_model_weight,
            MV_annotator_weight,
        ) = _get_consensus_stats(
            labels_multiannotator=labels_multiannotator,
            pred_probs=pred_probs,
            num_annotations=num_annotations,
            consensus_label=majority_vote_label,
            quality_method=quality_method,
            verbose=verbose,
            **label_quality_score_kwargs,
        )

    label_quality = pd.DataFrame({"num_annotations": num_annotations})
    valid_methods = ["majority_vote", "best_quality"]
    main_method = True

    for curr_method in consensus_method:
        # geting consensus label and stats
        if curr_method == "majority_vote":
            consensus_label = majority_vote_label
            annotator_agreement = MV_annotator_agreement
            consensus_quality_score = MV_consensus_quality_score
            post_pred_probs = MV_post_pred_probs
            model_weight = MV_model_weight
            annotator_weight = MV_annotator_weight

        elif curr_method == "best_quality":
            consensus_label = np.full(len(majority_vote_label), np.nan)
            for i in range(len(consensus_label)):
                max_pred_probs_ind = np.where(
                    MV_post_pred_probs[i] == np.max(MV_post_pred_probs[i])
                )[0]
                if len(max_pred_probs_ind) == 1:
                    consensus_label[i] = max_pred_probs_ind[0]
                else:
                    consensus_label[i] = majority_vote_label[i]
            consensus_label = consensus_label.astype("int64")  # convert all label types to int
            (
                annotator_agreement,
                consensus_quality_score,
                post_pred_probs,
                model_weight,
                annotator_weight,
            ) = _get_consensus_stats(
                labels_multiannotator=labels_multiannotator,
                pred_probs=pred_probs,
                num_annotations=num_annotations,
                consensus_label=consensus_label,
                quality_method=quality_method,
                verbose=verbose,
                **label_quality_score_kwargs,
            )

        else:
            raise ValueError(
                f"""
                {quality_method} is not a valid consensus method!
                Please choose a valid consensus_method: {valid_methods}
                """
            )

        # saving stats into dataframe, computing additional stats if specified
        if main_method:
            (
                label_quality["consensus_label"],
                label_quality["consensus_quality_score"],
                label_quality["annotator_agreement"],
            ) = (
                consensus_label,
                consensus_quality_score,
                annotator_agreement,
            )

            label_quality = label_quality.reindex(
                columns=[
                    "consensus_label",
                    "consensus_quality_score",
                    "annotator_agreement",
                    "num_annotations",
                ]
            )

            if return_detailed_quality:
                # Compute the label quality scores for each annotators' labels
                detailed_label_quality = labels_multiannotator.apply(
                    _get_annotator_label_quality_score,
                    pred_probs=post_pred_probs,
                    **label_quality_score_kwargs,
                )
                detailed_label_quality = detailed_label_quality.add_prefix("quality_annotator_")

            if return_annotator_stats:
                annotator_stats = _get_annotator_stats(
                    labels_multiannotator=labels_multiannotator,
                    pred_probs=post_pred_probs,
                    consensus_label=consensus_label,
                    num_annotations=num_annotations,
                    annotator_agreement=annotator_agreement,
                    model_weight=model_weight,
                    annotator_weight=annotator_weight,
                    consensus_quality_score=consensus_quality_score,
                    quality_method=quality_method,
                )

            main_method = False

        else:
            (
                label_quality[f"consensus_label_{curr_method}"],
                label_quality[f"consensus_quality_score_{curr_method}"],
                label_quality[f"annotator_agreement_{curr_method}"],
            ) = (
                consensus_label,
                consensus_quality_score,
                annotator_agreement,
            )

    if return_detailed_quality and return_annotator_stats:
        return {
            "label_quality": label_quality,
            "detailed_label_quality": detailed_label_quality,
            "annotator_stats": annotator_stats,
        }
    elif return_detailed_quality:
        return {
            "label_quality": label_quality,
            "detailed_label_quality": detailed_label_quality,
        }
    elif return_annotator_stats:
        return {
            "label_quality": label_quality,
            "annotator_stats": annotator_stats,
        }
    else:
        return {
            "label_quality": label_quality,
        }


def get_majority_vote_label(
    labels_multiannotator: Union[pd.DataFrame, np.ndarray],
    pred_probs: np.ndarray = None,
) -> np.ndarray:
    """Returns the majority vote label for each example, aggregated from the labels given by multiple annotators.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame or np.ndarray
        2D pandas DataFrame or array of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    pred_probs : np.ndarray, optional
        An array of shape ``(N, K)`` of model-predicted probabilities, ``P(label=k|x)``.
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.

    Returns
    -------
    consensus_label: np.ndarray
        An array of shape ``(N,)`` with the majority vote label aggregated from all annotators.
    """

    if isinstance(labels_multiannotator, np.ndarray):
        labels_multiannotator = pd.DataFrame(labels_multiannotator)

    majority_vote_label = np.full(len(labels_multiannotator), np.nan)
    mode_labels_multiannotator = labels_multiannotator.mode(axis=1)

    nontied_idx = []
    tied_idx = dict()

    # obtaining consensus using annotator majority vote
    for idx in range(len(mode_labels_multiannotator)):
        label_mode = mode_labels_multiannotator.iloc[idx].dropna().astype(int).to_numpy()
        if len(label_mode) == 1:
            majority_vote_label[idx] = label_mode[0]
            nontied_idx.append(idx)
        else:
            tied_idx[idx] = label_mode

    # tiebreak 1: using pred_probs (if provided)
    if pred_probs is not None and len(tied_idx) > 0:
        for idx, label_mode in tied_idx.copy().items():
            max_pred_probs = np.where(
                pred_probs[idx, label_mode] == np.max(pred_probs[idx, label_mode])
            )[0]
            if len(max_pred_probs) == 1:
                majority_vote_label[idx] = label_mode[max_pred_probs[0]]
                del tied_idx[idx]
            else:
                tied_idx[idx] = label_mode[max_pred_probs]

    # tiebreak 2: using empirical class frequencies
    if len(tied_idx) > 0:
        class_frequencies = (
            labels_multiannotator.apply(lambda s: s.value_counts(), axis=1).sum().to_numpy()
        )
        for idx, label_mode in tied_idx.copy().items():
            max_frequency = np.where(
                class_frequencies[label_mode] == np.max(class_frequencies[label_mode])
            )[0]
            if len(max_frequency) == 1:
                majority_vote_label[idx] = label_mode[max_frequency[0]]
                del tied_idx[idx]
            else:
                tied_idx[idx] = label_mode[max_frequency]

    # tiebreak 3: using initial annotator quality scores
    if len(tied_idx) > 0:
        nontied_majority_vote_label = majority_vote_label[nontied_idx]
        nontied_labels_multiannotator = labels_multiannotator.iloc[nontied_idx]
        annotator_agreement_with_consensus = nontied_labels_multiannotator.apply(
            lambda s: np.mean(s[pd.notna(s)] == nontied_majority_vote_label[pd.notna(s)]),
            axis=0,
        ).to_numpy()
        for idx, label_mode in tied_idx.copy().items():
            label_quality_score = np.array(
                [
                    np.mean(
                        annotator_agreement_with_consensus[
                            labels_multiannotator.iloc[idx][
                                labels_multiannotator.iloc[idx] == label
                            ].index.values
                        ]
                    )
                    for label in label_mode
                ]
            )
            max_score = np.where(label_quality_score == label_quality_score.max())[0]
            if len(max_score) == 1:
                majority_vote_label[idx] = label_mode[max_score[0]]
                del tied_idx[idx]
            else:
                tied_idx[idx] = label_mode[max_score]

    # if still tied, break by random selection
    if len(tied_idx) > 0:
        warnings.warn(
            f"breaking ties of examples {list(tied_idx.keys())} by random selection, you may want to set seed for reproducability"
        )
        for idx, label_mode in tied_idx.items():
            majority_vote_label[idx] = np.random.choice(label_mode)

    return majority_vote_label.astype("int64")


def _get_consensus_stats(
    labels_multiannotator: pd.DataFrame,
    pred_probs: np.ndarray,
    num_annotations: np.ndarray,
    consensus_label: np.ndarray,
    quality_method: str = "crowdlab",
    verbose: bool = True,
    label_quality_score_kwargs: dict = {},
) -> tuple:
    """Returns a tuple containing the consensus labels, annotator agreement scores, and quality of consensus

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    pred_probs : np.ndarray
        An array of shape ``(N, K)`` of model-predicted probabilities, ``P(label=k|x)``.
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    num_annotations : np.ndarray
        An array of shape ``(N,)`` with the number of annotators that have labeled each example.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    quality_method : str, default = "crowdlab" (Options: ["crowdlab", "agreement"])
        Specifies the method used to calculate the quality of the consensus label.
        For valid quality methods, view :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`
    label_quality_score_kwargs : dict, optional
        Keyword arguments to pass into ``get_label_quality_scores()``.
    verbose : bool, default = True
        Certain warnings and notes will be printed if ``verbose`` is set to ``True``.

    Returns
    ------
    tuple
        A tuple of (consensus_label, annotator_agreement, consensus_quality_score, post_pred_probs).
    """

    # compute the fraction of annotator agreeing with the consensus labels
    annotator_agreement = _get_annotator_agreement_with_consensus(
        labels_multiannotator=labels_multiannotator,
        consensus_label=consensus_label,
    )

    # compute posterior predicted probabilites
    post_pred_probs, model_weight, annotator_weight = _get_post_pred_probs_and_weights(
        labels_multiannotator=labels_multiannotator,
        consensus_label=consensus_label,
        prior_pred_probs=pred_probs,
        num_annotations=num_annotations,
        annotator_agreement=annotator_agreement,
        quality_method=quality_method,
        verbose=verbose,
    )

    # compute quality of the consensus labels
    consensus_quality_score = _get_consensus_quality_score(
        consensus_label=consensus_label,
        pred_probs=post_pred_probs,
        num_annotations=num_annotations,
        annotator_agreement=annotator_agreement,
        quality_method=quality_method,
        **label_quality_score_kwargs,
    )

    return (
        annotator_agreement,
        consensus_quality_score,
        post_pred_probs,
        model_weight,
        annotator_weight,
    )


def _get_annotator_stats(
    labels_multiannotator: pd.DataFrame,
    pred_probs: np.ndarray,
    consensus_label: np.ndarray,
    num_annotations: np.ndarray,
    annotator_agreement: np.ndarray,
    model_weight: np.ndarray,
    annotator_weight: np.ndarray,
    consensus_quality_score: np.ndarray,
    quality_method: str = "crowdlab",
) -> pd.DataFrame:
    """Returns a dictionary containing overall statistics about each annotator.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    pred_probs : np.ndarray
        An array of shape ``(N, K)`` of model-predicted probabilities, ``P(label=k|x)``.
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    num_annotations : np.ndarray
        An array of shape ``(N,)`` with the number of annotators that have labeled each example.
    annotator_agreement : np.ndarray
        An array of shape ``(N,)`` with the fraction of annotators that agree with each consensus label.
    model_weight : float
        float specifying the model weight used in weighted averages,
        None if model weight is not used to compute quality scores
    annotator_weight : np.ndarray
        An array of shape ``(M,)`` where M is the number of annotators, specifying the annotator weights used in weighted averages,
        None if annotator weights are not used to compute quality scores
    consensus_quality_score : np.ndarray
        An array of shape ``(N,)`` with the quality score of the consensus.
    quality_method : str, default = "crowdlab" (Options: ["crowdlab", "agreement"])
        Specifies the method used to calculate the quality of the consensus label.
        For valid quality methods, view :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`

    Returns
    -------
    annotator_stats : pd.DataFrame
        Returns overall statistics about each annotator.
        For details, see the documentation of :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    """

    annotator_quality = _get_annotator_quality(
        labels_multiannotator=labels_multiannotator,
        pred_probs=pred_probs,
        consensus_label=consensus_label,
        num_annotations=num_annotations,
        annotator_agreement=annotator_agreement,
        model_weight=model_weight,
        annotator_weight=annotator_weight,
        quality_method=quality_method,
    )

    # Compute the number of labels labeled/ by each annotator
    num_examples_labeled = labels_multiannotator.count()

    # Compute the fraction of labels annotated by each annotator that agrees with the consensus label
    # TODO: check if we should drop singleton labels here
    agreement_with_consensus = labels_multiannotator.apply(
        lambda s: np.mean(s[pd.notna(s)] == consensus_label[pd.notna(s)]),
        axis=0,
    ).to_numpy()

    # Find the worst labeled class for each annotator
    worst_class = _get_annotator_worst_class(
        labels_multiannotator=labels_multiannotator,
        consensus_label=consensus_label,
        consensus_quality_score=consensus_quality_score,
    )

    # Create multi-annotator stats DataFrame from its columns
    annotator_stats = pd.DataFrame(
        {
            "annotator_quality": annotator_quality,
            "agreement_with_consensus": agreement_with_consensus,
            "worst_class": worst_class,
            "num_examples_labeled": num_examples_labeled,
        }
    )

    return annotator_stats.sort_values(by=["annotator_quality", "agreement_with_consensus"])


def _get_annotator_agreement_with_consensus(
    labels_multiannotator: pd.DataFrame,
    consensus_label: np.ndarray,
) -> np.ndarray:
    """Returns the fractions of annotators that agree with the consensus label per example. Note that the
    fraction for each example only considers the annotators that labeled that particular example.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.

    Returns
    -------
    annotator_agreement : np.ndarray
        An array of shape ``(N,)`` with the fraction of annotators that agree with each consensus label.
    """
    annotator_agreement = labels_multiannotator.assign(consensus_label=consensus_label).apply(
        lambda s: np.mean(s.drop("consensus_label").dropna() == s["consensus_label"]),
        axis=1,
    )
    return annotator_agreement.to_numpy()


def _get_annotator_agreement_with_annotators(
    labels_multiannotator: pd.DataFrame,
    num_annotations: np.ndarray,
    verbose: bool = True,
) -> np.ndarray:
    """Returns the average agreement of each annotator with other annotators that label the same example.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    verbose : bool, default = True
        Certain warnings and notes will be printed if ``verbose`` is set to ``True``.

    Returns
    -------
    annotator_agreement : np.ndarray
        An array of shape ``(M,)`` where M is the number of annotators, with the agreement of each annotator with other
        annotators that labeled the same examples.
    """

    def get_single_annotator_agreement(
        labels_multiannotator: pd.DataFrame,
        num_annotations: np.ndarray,
        annotator_id: Union[int, str],
    ):
        annotator_agreement_per_example = labels_multiannotator.apply(
            lambda s: np.mean(s[pd.notna(s)].drop(annotator_id) == s[annotator_id]), axis=1
        )
        np.nan_to_num(annotator_agreement_per_example, copy=False, nan=0)
        try:
            annotator_agreement = np.average(
                annotator_agreement_per_example, weights=num_annotations - 1
            )
        except:
            annotator_agreement = np.NaN
        return annotator_agreement

    annotator_agreement_with_annotators = labels_multiannotator.apply(
        lambda s: get_single_annotator_agreement(
            labels_multiannotator[pd.notna(s)], num_annotations[pd.notna(s)], s.name
        )
    )

    # impute average annotator accuracy for any annotator that do not overlap with other annotators
    mask = annotator_agreement_with_annotators.isna()
    if np.sum(mask) > 0:
        if verbose:
            print(
                f"Annotator(s) {annotator_agreement_with_annotators[mask].index.values} did not annotate any examples that overlap with other annotators, \
                \nusing the average annotator agreeement among other annotators as this annotator's agreement."
            )

        avg_annotator_agreement = np.mean(annotator_agreement_with_annotators[~mask])
        annotator_agreement_with_annotators[mask] = avg_annotator_agreement

    return annotator_agreement_with_annotators.to_numpy()


def _get_post_pred_probs_and_weights(
    labels_multiannotator: pd.DataFrame,
    consensus_label: np.ndarray,
    prior_pred_probs: np.ndarray,
    num_annotations: np.ndarray,
    annotator_agreement: np.ndarray,
    quality_method: str = "crowdlab",
    verbose: bool = True,
) -> Tuple[np.ndarray, Any, Any]:
    """Return the posterior predicted probabilites of each example given a specified quality method.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape ``(N, M)``,
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    prior_pred_probs : np.ndarray
        An array of shape ``(N, K)`` of prior predicted probabilities, ``P(label=k|x)``, usually the out-of-sample predicted probability computed by a model.
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    num_annotations : np.ndarray
        An array of shape ``(N,)`` with the number of annotators that have labeled each example.
    annotator_agreement : np.ndarray
        An array of shape ``(N,)`` with the fraction of annotators that agree with each consensus label.
    quality_method : str, default = "crowdlab" (Options: ["crowdlab", "agreement"])
        Specifies the method used to calculate the quality of the consensus label.
        For valid quality methods, view :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`
    verbose : bool, default = True
        Certain warnings and notes will be printed if ``verbose`` is set to ``True``.

    Returns
    -------
    post_pred_probs : np.ndarray
        An array of shape ``(N, K)`` with the posterior predicted probabilities.

    model_weight : float
        float specifying the model weight used in weighted averages,
        None if model weight is not used to compute quality scores

    annotator_weight : np.ndarray
        An array of shape ``(M,)`` where M is the number of annotators, specifying the annotator weights used in weighted averages,
        None if annotator weights are not used to compute quality scores

    """
    valid_methods = [
        "crowdlab",
        "agreement",
    ]

    # setting dummy variables for model and annotator weights that will be returned
    # only relevant for quality_method == crowdlab, return None for all other methods
    return_model_weight = None
    return_annotator_weight = None

    if quality_method == "crowdlab":
        num_classes = get_num_classes(pred_probs=prior_pred_probs)
        likelihood = np.mean(
            annotator_agreement[num_annotations != 1]
        )  # likelihood that any annotator will annotate the consensus label for any example

        # subsetting the dataset to only includes examples with more than one annotation
        mask = num_annotations != 1
        consensus_label_subset = consensus_label[mask]
        prior_pred_probs_subset = prior_pred_probs[mask]

        # compute most likely class error
        most_likely_class_error = np.mean(
            consensus_label_subset
            != np.argmax(np.bincount(consensus_label_subset, minlength=num_classes))
        )

        # compute adjusted annotator agreement (used as annotator weights)
        annotator_agreement_with_annotators = _get_annotator_agreement_with_annotators(
            labels_multiannotator, num_annotations, verbose
        )
        annotator_error = 1 - annotator_agreement_with_annotators
        adjusted_annotator_agreement = np.clip(
            1 - (annotator_error / most_likely_class_error), a_min=1e-6, a_max=None
        )

        # compute model weight
        model_error = np.mean(np.argmax(prior_pred_probs_subset, axis=1) != consensus_label_subset)
        model_weight = np.max([(1 - (model_error / most_likely_class_error)), 1e-6]) * np.sqrt(
            np.mean(num_annotations)
        )

        # compute weighted average
        post_pred_probs = np.full(prior_pred_probs.shape, np.nan)
        for i in range(len(labels_multiannotator)):
            example_mask = labels_multiannotator.iloc[i].notna()
            example = labels_multiannotator.iloc[i][example_mask]
            post_pred_probs[i] = [
                np.average(
                    [prior_pred_probs[i, true_label]]
                    + [
                        likelihood
                        if annotator_label == true_label
                        else (1 - likelihood) / (num_classes - 1)
                        for annotator_label in example
                    ],
                    weights=np.concatenate(
                        ([model_weight], adjusted_annotator_agreement[example_mask])
                    ),
                )
                for true_label in range(num_classes)
            ]

        return_model_weight = model_weight
        return_annotator_weight = adjusted_annotator_agreement

    elif quality_method == "agreement":
        label_counts = (
            labels_multiannotator.apply(lambda s: s.value_counts(), axis=1)
            .fillna(value=0)
            .to_numpy()
        )
        post_pred_probs = label_counts / num_annotations.reshape(-1, 1)

    else:
        raise ValueError(
            f"""
            {quality_method} is not a valid quality method!
            Please choose a valid quality_method: {valid_methods}
            """
        )

    return post_pred_probs, return_model_weight, return_annotator_weight


def _get_consensus_quality_score(
    consensus_label: np.ndarray,
    pred_probs: np.ndarray,
    num_annotations: np.ndarray,
    annotator_agreement: np.ndarray,
    quality_method: str = "crowdlab",
    label_quality_score_kwargs: dict = {},
) -> np.ndarray:
    """Return scores representing quality of the consensus label for each example.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    pred_probs : np.ndarray
        An array of shape ``(N, K)`` of posterior predicted probabilities, ``P(label=k|x)``.
        Posterior
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    num_annotations : np.ndarray
        An array of shape ``(N,)`` with the number of annotators that have labeled each example.
    annotator_agreement : np.ndarray
        An array of shape ``(N,)`` with the fraction of annotators that agree with each consensus label.
    quality_method : str, default = "crowdlab" (Options: ["crowdlab", "agreement"])
        Specifies the method used to calculate the quality of the consensus label.
        For valid quality methods, view :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`

    Returns
    -------
    consensus_quality_score : np.ndarray
        An array of shape ``(N,)`` with the quality score of the consensus.
    """

    valid_methods = [
        "crowdlab",
        "agreement",
    ]

    if quality_method == "crowdlab":
        consensus_quality_score = get_label_quality_scores(
            consensus_label, pred_probs, **label_quality_score_kwargs
        )

    elif quality_method == "agreement":
        consensus_quality_score = annotator_agreement

    else:
        raise ValueError(
            f"""
            {quality_method} is not a valid consensus quality method!
            Please choose a valid quality_method: {valid_methods}
            """
        )

    return consensus_quality_score


def _get_annotator_label_quality_score(
    annotator_label: pd.Series,
    pred_probs: np.ndarray,
    label_quality_score_kwargs: dict = {},
) -> np.ndarray:
    """Returns quality scores for each datapoint.
    Very similar functionality as ``_get_consensus_quality_score`` with additional support for annotator labels that contain NaN values.
    For more info about parameters and returns, see the docstring of :py:func:`_get_consensus_quality_score <cleanlab.multiannotator._get_consensus_quality_score>`.
    """
    mask = pd.notna(annotator_label)

    annotator_label_quality_score_subset = get_label_quality_scores(
        labels=annotator_label[mask].to_numpy().astype("int64"),
        pred_probs=pred_probs[mask],
        **label_quality_score_kwargs,
    )

    annotator_label_quality_score = np.full(len(annotator_label), np.nan)
    annotator_label_quality_score[mask] = annotator_label_quality_score_subset
    return annotator_label_quality_score


def _get_annotator_quality(
    labels_multiannotator: pd.DataFrame,
    pred_probs: np.ndarray,
    consensus_label: np.ndarray,
    num_annotations: np.ndarray,
    annotator_agreement: np.ndarray,
    model_weight: np.ndarray,
    annotator_weight: np.ndarray,
    quality_method: str = "crowdlab",
) -> pd.DataFrame:
    """Returns annotator quality score for each annotator.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    pred_probs : np.ndarray
        An array of shape ``(N, K)`` of model-predicted probabilities, ``P(label=k|x)``.
        For details, predicted probabilities in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    num_annotations : np.ndarray
        An array of shape ``(N,)`` with the number of annotators that have labeled each example.
    annotator_agreement : np.ndarray
        An array of shape ``(N,)`` with the fraction of annotators that agree with each consensus label.
    model_weight : float
        float specifying the model weight used in weighted averages,
        None if model weight is not used to compute quality scores
    annotator_weight : np.ndarray
        An array of shape ``(M,)`` where M is the number of annotators, specifying the annotator weights used in weighted averages,
        None if annotator weights are not used to compute quality scores
    quality_method : str, default = "crowdlab" (Options: ["crowdlab", "agreement"])
        Specifies the method used to calculate the quality of the annotators.
        For valid quality methods, view :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`

    Returns
    -------
    annotator_quality : np.ndarray
        Quality scores of a given annotator's labels
    """

    valid_methods = [
        "crowdlab",
        "agreement",
    ]

    if quality_method == "crowdlab":
        annotator_lqs = labels_multiannotator.apply(
            lambda s: np.mean(
                get_label_quality_scores(s[pd.notna(s)].astype("int64"), pred_probs[pd.notna(s)])
            )
        )

        mask = num_annotations != 1
        labels_multiannotator_subset = labels_multiannotator[mask]
        consensus_label_subset = consensus_label[mask]

        annotator_agreement = labels_multiannotator_subset.apply(
            lambda s: np.mean(s[pd.notna(s)] == consensus_label_subset[pd.notna(s)]),
            axis=0,
        )

        avg_num_annotations_frac = np.mean(num_annotations) / len(annotator_weight)
        annotator_weight_adjusted = np.sum(annotator_weight) * avg_num_annotations_frac

        w = model_weight / (model_weight + annotator_weight_adjusted)
        annotator_quality = w * annotator_lqs + (1 - w) * annotator_agreement

    elif quality_method == "agreement":
        mask = num_annotations != 1
        labels_multiannotator_subset = labels_multiannotator[mask]
        consensus_label_subset = consensus_label[mask]

        annotator_quality = labels_multiannotator_subset.apply(
            lambda s: np.mean(s[pd.notna(s)] == consensus_label_subset[pd.notna(s)]),
            axis=0,
        )

    else:
        raise ValueError(
            f"""
            {quality_method} is not a valid annotator quality method!
            Please choose a valid quality_method: {valid_methods}
            """
        )

    return annotator_quality


def _get_annotator_worst_class(
    labels_multiannotator: pd.DataFrame,
    consensus_label: np.ndarray,
    consensus_quality_score: np.ndarray,
) -> np.ndarray:
    """Returns the class which each annotator makes the most errors in.

    Parameters
    ----------
    labels_multiannotator : pd.DataFrame
        2D pandas DataFrame of multiple given labels for each example with shape (N, M),
        where N is the number of examples and M is the number of annotators.
        For more details, labels in the same format expected by the :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    consensus_label : np.ndarray
        An array of shape ``(N,)`` with the consensus labels aggregated from all annotators.
    consensus_quality_score : np.ndarray
        An array of shape ``(N,)`` with the quality score of the consensus.

    Returns
    -------
    worst_class : np.ndarray
        The class that is most frequently mislabeled by a given annotator
    """

    def get_single_annotator_worst_class(s, consensus_quality_score):
        mask = pd.notna(s)
        class_accuracies = (s[mask] == consensus_label[mask]).groupby(s).mean()
        accuracy_min_idx = class_accuracies[class_accuracies == class_accuracies.min()].index.values

        if len(accuracy_min_idx) == 1:
            return accuracy_min_idx[0]

        # tiebreak 1: class counts
        class_count = s[mask].groupby(s).count()[accuracy_min_idx]
        count_max_idx = class_count[class_count == class_count.max()].index.values

        if len(count_max_idx) == 1:
            return count_max_idx[0]

        # tiebreak 2: consensus quality scores
        avg_consensus_quality = (
            pd.DataFrame(
                {"annotator_label": s, "consensus_quality_score": consensus_quality_score}
            )[mask]
            .groupby("annotator_label")
            .mean()["consensus_quality_score"][count_max_idx]
        )
        quality_max_ind = avg_consensus_quality[
            avg_consensus_quality == avg_consensus_quality.max()
        ].index.values

        # return first item even if there are ties - no better methods to tiebreak
        return quality_max_ind[0]

    worst_class = (
        labels_multiannotator.apply(
            lambda s: get_single_annotator_worst_class(s, consensus_quality_score)
        )
        .to_numpy()
        .astype("int64")
    )

    return worst_class


def convert_long_to_wide_dataset(
    labels_multiannotator_long: pd.DataFrame,
) -> pd.DataFrame:
    """Converts a long format dataset to wide format which is suitable for passing into
    :py:func:`get_label_quality_multiannotator <cleanlab.multiannotator.get_label_quality_multiannotator>`.
    Dataframe must contain three columns named:
    1. ``task`` representing each example labeled by the annotators
    2. ``annotator`` representing each annotator
    3. ``label`` representing the label given by an annotator for the corresponding task

    Parameters
    ----------
    labels_multiannotator_long : pd.DataFrame
        pandas DataFrame in long format with three columns named ``task``, ``annotator`` and ``label``

    Returns
    -------
    labels_multiannotator_wide : pd.DataFrame
        pandas DataFrame of the proper format to be passed as ``labels_multiannotator`` for the other ``cleanlab.multiannotator`` functions.
    """
    labels_multiannotator_wide = labels_multiannotator_long.pivot(
        index="task", columns="annotator", values="label"
    )
    labels_multiannotator_wide.index.name = None
    labels_multiannotator_wide.columns.name = None
    return labels_multiannotator_wide
